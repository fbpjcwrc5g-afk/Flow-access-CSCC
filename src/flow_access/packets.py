"""Plan-specific post-acute referral packets: documents, contracted agencies, and PCS forms."""

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from .clinical import NeedDetector, TransportCriteria, diagnoses
from .fhir.extract import ref_id
from .models import NeedType, Pathway, PatientContext, PlanMatch
from .pcs import PcsForm, TransportRequest, build_pcs
from .plans import Agency, PcsFormConfig, PlanDirectory, PlanProfile

Resource = dict[str, Any]


class NeedPacket(BaseModel):
    need: NeedType
    ordered: bool
    order_ids: list[str] = Field(default_factory=list)
    authorization_contact: str = ""
    documents: list[str]
    agencies: list[Agency]
    agency_network: str | None = None
    pcs_form: PcsForm | None = None


class CarePacket(BaseModel):
    patient_id: str
    pathway: Pathway
    plans: list[PlanMatch]
    managing_entity: str | None
    needs: list[NeedPacket]


class PacketBuilder:
    def __init__(self, directory: PlanDirectory, criteria: TransportCriteria):
        self.directory = directory
        self.criteria = criteria
        self.detector = NeedDetector(directory.need_keywords)

    def build(
        self,
        context: PatientContext,
        pathway: Pathway,
        plans: list[PlanMatch],
        needs: list[NeedType] | None = None,
        transport: TransportRequest | None = None,
        encounter_id: str | None = None,
        now: datetime | None = None,
    ) -> CarePacket:
        ordered: dict[NeedType, list[str]] = {}
        for event in self.detector.events(context.service_requests):
            ordered.setdefault(event.need, []).append(event.source_id)
        # With no explicit request, show every service so the case manager sees the plan's full network.
        wanted = needs or list(NeedType)
        profile = self.directory.managing_profile(plans)
        return CarePacket(
            patient_id=context.patient_id,
            pathway=pathway,
            plans=plans,
            managing_entity=profile.display_name if profile else None,
            needs=[
                self.need_packet(context, plans, profile, need, ordered.get(need, []), transport, encounter_id, now)
                for need in wanted
            ],
        )

    def need_packet(
        self,
        context: PatientContext,
        plans: list[PlanMatch],
        profile: PlanProfile | None,
        need: NeedType,
        order_ids: list[str],
        transport: TransportRequest | None = None,
        encounter_id: str | None = None,
        now: datetime | None = None,
    ) -> NeedPacket:
        network, agencies = self.directory.agencies_for(plans, need)
        packet = NeedPacket(
            need=need,
            ordered=bool(order_ids),
            order_ids=order_ids,
            authorization_contact=(
                (profile.transportation_contact if need == NeedType.TRANSPORTATION else profile.authorization_contact)
                if profile else ""
            ),
            documents=self.directory.documents_for(profile, need),
            agencies=agencies,
            agency_network=network.display_name if network else None,
        )
        if need == NeedType.TRANSPORTATION:
            packet.pcs_form = self.pcs(context, plans, profile, order_ids, transport, encounter_id, now)
        return packet

    def pcs(
        self,
        context: PatientContext,
        plans: list[PlanMatch],
        profile: PlanProfile | None,
        order_ids: list[str] | None = None,
        transport: TransportRequest | None = None,
        encounter_id: str | None = None,
        now: datetime | None = None,
    ) -> PcsForm:
        dx = diagnoses(context.conditions, encounter_id)
        plan = next((m for m in plans if m.role == "health_plan"), plans[0] if plans else None)
        return build_pcs(
            patient=context.patient,
            dx=dx,
            assessment=self.criteria.assess(dx),
            criteria=self.criteria,
            form_config=profile.pcs_form if profile else PcsFormConfig(),
            plan_name=plan.display_name if plan else "",
            member_id=plan.member_id if plan else None,
            prescriber=self._prescriber(context, order_ids or []),
            request=transport,
            now=now,
        )

    @staticmethod
    def _prescriber(context: PatientContext, order_ids: list[str]) -> Resource | None:
        """The requester of the transport order, else the requester of any active order."""
        by_id = {p.get("id"): p for p in context.practitioners}
        orders = sorted(context.service_requests, key=lambda o: o.get("id") not in order_ids)
        for order in orders:
            pid = ref_id(order.get("requester"), "Practitioner")
            if pid in by_id:
                return by_id[pid]
        return None

    def task_details(self, packet: NeedPacket) -> dict:
        """Compact summary attached to a case manager task."""
        details: dict[str, Any] = {
            "documents": packet.documents,
            "agencies": [a.model_dump(exclude_defaults=True) for a in packet.agencies],
            "agency_network": packet.agency_network,
            "authorization_contact": packet.authorization_contact,
            "order_ids": packet.order_ids,
        }
        if packet.pcs_form:
            details["pcs_form"] = {
                "form_name": packet.pcs_form.form_name,
                "recommended_mode": packet.pcs_form.recommended_mode,
                "diagnoses": [f"{d.code} {d.display}" for d in packet.pcs_form.diagnoses],
                "missing": packet.pcs_form.missing,
            }
        return details
