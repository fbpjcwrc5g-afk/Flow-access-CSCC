import argparse
import json
import logging
import sys
import time
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="flow-access")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the HTTP API (notifications, evaluation, JWKS)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)

    poll = sub.add_parser("poll", help="Run incremental Bulk FHIR $export polling against Epic")
    poll.add_argument("--once", action="store_true", help="Run a single export cycle and exit")
    poll.add_argument("--interval", type=int, default=900, help="Seconds between cycles")

    evaluate = sub.add_parser("evaluate", help="Evaluate a FHIR Bundle file offline (dry run)")
    evaluate.add_argument("bundle", type=Path)

    keygen = sub.add_parser("generate-key", help="Create an RSA key pair for Epic backend services")
    keygen.add_argument("--out", type=Path, default=Path("epic_private_key.pem"))

    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")

    if args.command == "serve":
        import uvicorn

        from .api import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port)
    elif args.command == "poll":
        return _poll(args.once, args.interval)
    elif args.command == "evaluate":
        return _evaluate(args.bundle)
    elif args.command == "generate-key":
        return _generate_key(args.out)
    return 0


def _poll(once: bool, interval: int) -> int:
    from .settings import get_settings
    from .wiring import build_auth, build_service

    settings = get_settings()
    if not settings.epic_bulk_group_id:
        print("FLOW_EPIC_BULK_GROUP_ID is required for polling", file=sys.stderr)
        return 2
    service = build_service(settings, build_auth(settings))
    while True:
        try:
            evaluations = service.poll_bulk(settings.epic_bulk_group_id)
            dispatched = sum(len(e.dispatched) for e in evaluations)
            logging.info("Poll cycle evaluated %d patients, dispatched %d tasks", len(evaluations), dispatched)
        except Exception:
            logging.exception("Poll cycle failed")
            if once:
                return 1
        if once:
            return 0
        time.sleep(interval)


def _evaluate(bundle_path: Path) -> int:
    from .api import context_from_bundle
    from .pathway import PathwayClassifier
    from .settings import get_settings
    from .workflows import WorkflowEngine, encounter_events, load_rules

    settings = get_settings()
    context = context_from_bundle(json.loads(bundle_path.read_text()))
    determination = PathwayClassifier.from_file(settings.pathway_rules_path).determine(context.coverages)
    events = [e for enc in context.encounters for e in encounter_events(enc)]
    tasks = WorkflowEngine(load_rules(settings.workflow_rules_path)).tasks_for(context, determination, events)
    print(json.dumps(
        {
            "patient_id": context.patient_id,
            "determination": determination.model_dump(mode="json"),
            "events": [e.model_dump(mode="json") for e in events],
            "tasks": [t.model_dump(mode="json", exclude={"dedup_key"}) for t in tasks],
        },
        indent=2,
    ))
    return 0


def _generate_key(out: Path) -> int:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    if out.exists():
        print(f"{out} already exists; refusing to overwrite", file=sys.stderr)
        return 1
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    out.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ))
    out.chmod(0o600)
    public = out.with_suffix(".pub.pem")
    public.write_bytes(key.public_key().public_bytes(
        serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
    ))
    print(f"Wrote private key to {out} and public key to {public}")
    print("Register the public key (or the /.well-known/jwks.json URL) with your Epic app.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
