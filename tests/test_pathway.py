from datetime import datetime, timezone

from conftest import coverage

from flow_access.models import Pathway

NOW = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def test_sopt_coding_uses_longest_prefix(classifier):
    assert classifier.determine([coverage("c1", "CMS", code="1")], NOW).pathway == Pathway.MEDICARE_TRADITIONAL
    result = classifier.determine([coverage("c1", "Some Plan", code="111")], NOW)
    assert result.pathway == Pathway.MEDICARE_ADVANTAGE
    assert result.confidence == "high"
    assert not result.needs_verification


def test_hl7_actcode_workers_comp(classifier):
    cov = coverage("c1", "State Fund", code="WCBPOL", system="http://terminology.hl7.org/CodeSystem/v3-ActCode")
    assert classifier.determine([cov], NOW).pathway == Pathway.WORKERS_COMP


def test_keyword_match_prefers_specific_pathways(classifier):
    result = classifier.determine([coverage("c1", "Humana", plan="Humana Gold Medicare Advantage HMO")], NOW)
    assert result.pathway == Pathway.MEDICARE_ADVANTAGE
    assert result.confidence == "medium"
    assert result.needs_verification

    medicaid_mco = classifier.determine([coverage("c1", "UnitedHealthcare Community Plan")], NOW)
    assert medicaid_mco.pathway == Pathway.MEDICAID_MANAGED_CARE


def test_medicare_plus_medicaid_is_dual(classifier):
    result = classifier.determine(
        [coverage("c1", "Medicare Part A/B", code="1", order=1), coverage("c2", "Medi-Cal", code="2", order=2)], NOW
    )
    assert result.pathway == Pathway.DUAL_ELIGIBLE
    assert result.primary.coverage_id == "c1"


def test_primary_coverage_order_wins(classifier):
    result = classifier.determine(
        [coverage("c2", "AARP Medigap", code="5", order=2), coverage("c1", "Medicare", code="1", order=1)], NOW
    )
    assert result.pathway == Pathway.MEDICARE_TRADITIONAL
    assert [c.coverage_id for c in result.coverages] == ["c1", "c2"]


def test_inactive_and_expired_coverage_ignored(classifier):
    result = classifier.determine(
        [coverage("c1", "Aetna PPO", code="5", end="2025-12-31"), coverage("c2", "Cigna", code="5", status="cancelled")],
        NOW,
    )
    assert result.pathway == Pathway.SELF_PAY
    assert result.needs_verification


def test_unclassifiable_coverage_is_unknown(classifier):
    result = classifier.determine([coverage("c1", "Acme Benefits Administrators")], NOW)
    assert result.pathway == Pathway.UNKNOWN
    assert result.confidence == "low"
