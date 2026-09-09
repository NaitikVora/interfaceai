"""Hand-authored artifacts mirroring what the recorder produces, for deterministic tests."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

from app.artifacts.profile import ApplicationProfile
from app.artifacts.schema import (
    ActionType,
    ArtifactMetadata,
    CapabilityArtifact,
    Checkpoint,
    Compatibility,
    ElementValue,
    ExtractSource,
    HeadingPresent,
    InputSource,
    InputSpec,
    OnError,
    OutputSpec,
    PolicyRequirements,
    RiskClass,
    RuleMessageSource,
    StatusSource,
    Step,
    StepArguments,
    TableCellSpec,
    TargetApplication,
    TargetSpec,
    UrlMatches,
    ValueType,
)

PROFILE_PATH = Path(__file__).resolve().parents[2] / "profiles" / "demobank_legacycore.json"

NAV = TargetSpec(attributes={"tag": "td", "class": "nav"})
OPERATOR_FIELD = TargetSpec(
    role="textbox",
    name="Operator ID:",
    label="Operator ID:",
    attributes={"tag": "input", "name": "operator_id", "type": "text"},
)
ACCESS_FIELD = TargetSpec(
    role="textbox",
    name="Access Code:",
    label="Access Code:",
    attributes={"tag": "input", "name": "access_code", "type": "password"},
)
MEMBER_FIELD = TargetSpec(
    role="textbox", name="Member Number", attributes={"tag": "input", "name": "member_no"}
)
SIGN_IN = TargetSpec(role="button", name="Sign In", attributes={"tag": "input", "value": "Sign In"})
SEARCH = TargetSpec(role="button", name="Search", attributes={"tag": "input", "value": "Search"})
MENU_LINK = TargetSpec(
    role="link",
    name="Member Search",
    within=NAV,
    attributes={"tag": "a", "href": "/members/search"},
)

LOGIN_PAGE = [UrlMatches(pattern="/login$"), HeadingPresent(text="Operator Sign In")]
HOME_PAGE = [UrlMatches(pattern="/home$"), HeadingPresent(text="Main Menu")]
SEARCH_PAGE = [UrlMatches(pattern="/members/search$"), HeadingPresent(text="Member Search")]
DETAILS_PAGE = [UrlMatches(pattern="/members/${member_id}$"), HeadingPresent(text="Member Details")]


def login_and_lookup_steps() -> list[Step]:
    return [
        Step(
            id="s01-navigate",
            action=ActionType.NAVIGATE,
            description="Open the sign-in page",
            arguments=StepArguments(url="${base_url}/login"),
            postconditions=LOGIN_PAGE,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s02-type-operator",
            action=ActionType.TYPE,
            description="Enter the operator id",
            target=OPERATOR_FIELD,
            arguments=StepArguments(value="${operator_id}"),
            preconditions=LOGIN_PAGE,
            postconditions=[ElementValue(target=OPERATOR_FIELD, value="${operator_id}")],
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s03-type-code",
            action=ActionType.TYPE,
            description="Enter the access code",
            target=ACCESS_FIELD,
            arguments=StepArguments(value="${access_code}"),
            preconditions=LOGIN_PAGE,
            postconditions=[ElementValue(target=ACCESS_FIELD, value="${access_code}")],
            risk_class=RiskClass.SENSITIVE,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s04-sign-in",
            action=ActionType.CLICK,
            description="Sign in",
            target=SIGN_IN,
            preconditions=LOGIN_PAGE,
            postconditions=HOME_PAGE,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s05-open-search",
            action=ActionType.CLICK,
            description="Open member search",
            target=MENU_LINK,
            preconditions=HOME_PAGE,
            postconditions=SEARCH_PAGE,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s06-type-member",
            action=ActionType.TYPE,
            description="Enter the member number",
            target=MEMBER_FIELD,
            arguments=StepArguments(value="${member_id}"),
            preconditions=SEARCH_PAGE,
            postconditions=[ElementValue(target=MEMBER_FIELD, value="${member_id}")],
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s07-search",
            action=ActionType.CLICK,
            description="Run the search",
            target=SEARCH,
            preconditions=SEARCH_PAGE,
            postconditions=DETAILS_PAGE,
            on_error=OnError.ESCALATE,
        ),
    ]


def _common(
    *,
    name: str,
    description: str,
    base_url: str,
    steps: list[Step],
    inputs: dict[str, InputSpec],
    outputs: dict[str, OutputSpec],
    checkpoint: Checkpoint,
    max_risk: RiskClass,
    step_timeout_s: float,
) -> CapabilityArtifact:
    profile = ApplicationProfile.load(PROFILE_PATH)
    steps = [s.model_copy(update={"timeout_s": step_timeout_s}) for s in steps]
    return CapabilityArtifact(
        artifact_id=str(uuid.uuid4()),
        name=name,
        version=1,
        description=description,
        target=TargetApplication(
            vendor="DemoBank",
            application="LegacyCore",
            version="1.4.2",
            base_url=base_url,
            entry_path="/login",
        ),
        inputs=inputs,
        outputs={
            "status": OutputSpec(
                type=ValueType.STRING, enum=profile.status_values(), source=StatusSource()
            ),
            "member_id": OutputSpec(type=ValueType.STRING, source=InputSource(name="member_id")),
            **outputs,
            "message": OutputSpec(type=ValueType.STRING, nullable=True, source=RuleMessageSource()),
        },
        steps=steps,
        checkpoint=checkpoint,
        conditions=profile.conditions,
        policy=PolicyRequirements(
            actions=sorted({s.action for s in steps}, key=lambda a: a.value),
            url_patterns=[f"^{base_url}(/.*)?$"],
            max_risk_class=max_risk,
            requires_human_confirmation=max_risk is RiskClass.IRREVERSIBLE,
        ),
        compatibility=Compatibility(
            vendor="DemoBank", product="LegacyCore", supported_versions=["1.x"]
        ),
        metadata=ArtifactMetadata(goal=description, discovery_run_id="hand-authored"),
        created_at=datetime.now(UTC),
    )


def savings_lookup_artifact(base_url: str, *, step_timeout_s: float = 5.0) -> CapabilityArtifact:
    steps = [
        *login_and_lookup_steps(),
        Step(
            id="s08-extract-savings",
            action=ActionType.EXTRACT,
            description="Read the savings balance",
            target=TargetSpec(
                table_cell=TableCellSpec(
                    row_match="Savings",
                    column_header="Current Balance",
                    table_headers=["Account Type", "Current Balance"],
                )
            ),
            arguments=StepArguments(output="savings_balance", value_type=ValueType.DECIMAL),
            preconditions=DETAILS_PAGE,
            on_error=OnError.ESCALATE,
        ),
    ]
    return _common(
        name="member_savings_lookup",
        description="Look up a member by member number and read the current savings balance.",
        base_url=base_url,
        steps=steps,
        inputs={
            "member_id": InputSpec(description="Five-digit member number", pattern=r"^\d{5}$"),
            "operator_id": InputSpec(description="Operator sign-in id"),
            "access_code": InputSpec(description="Operator access code", sensitive=True),
        },
        outputs={
            "savings_balance": OutputSpec(
                type=ValueType.DECIMAL,
                nullable=True,
                source=ExtractSource(step_id="s08-extract-savings"),
            )
        },
        checkpoint=Checkpoint(
            description="Member details for the requested member are shown",
            conditions=[*DETAILS_PAGE, HeadingPresent(text="Member Details")],
        ),
        max_risk=RiskClass.SENSITIVE,
        step_timeout_s=step_timeout_s,
    )


def subaccount_artifact(base_url: str, *, step_timeout_s: float = 5.0) -> CapabilityArtifact:
    nickname_field = TargetSpec(
        role="textbox", attributes={"tag": "input", "name": "nickname"}, description="nickname"
    )
    new_page = [
        UrlMatches(pattern="/members/${member_id}/subaccounts/new$"),
        HeadingPresent(text="Open New Sub-Account"),
    ]
    review_page = [
        UrlMatches(pattern="/members/${member_id}/subaccounts/review$"),
        HeadingPresent(text="Review Sub-Account Request"),
    ]
    confirmed_page = [
        UrlMatches(pattern="/members/${member_id}/subaccounts/confirmation/"),
        HeadingPresent(text="Sub-Account Opened"),
    ]
    steps = [
        *login_and_lookup_steps(),
        Step(
            id="s08-open-subaccount",
            action=ActionType.CLICK,
            description="Start opening a sub-account",
            target=TargetSpec(role="link", name="Open New Sub-Account"),
            preconditions=DETAILS_PAGE,
            postconditions=new_page,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s09-type-nickname",
            action=ActionType.TYPE,
            description="Enter the nickname",
            target=nickname_field,
            arguments=StepArguments(value="${nickname}"),
            preconditions=new_page,
            postconditions=[ElementValue(target=nickname_field, value="${nickname}")],
            risk_class=RiskClass.SENSITIVE,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s10-continue",
            action=ActionType.CLICK,
            description="Continue to review",
            target=TargetSpec(role="button", name="Continue"),
            preconditions=new_page,
            postconditions=review_page,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s11-confirm",
            action=ActionType.CLICK,
            description="Confirm and open the account",
            target=TargetSpec(role="button", name="Confirm and Open Account"),
            preconditions=review_page,
            postconditions=confirmed_page,
            risk_class=RiskClass.IRREVERSIBLE,
            on_error=OnError.ESCALATE,
        ),
        Step(
            id="s12-extract-confirmation",
            action=ActionType.EXTRACT,
            description="Read the confirmation number",
            target=TargetSpec(
                table_cell=TableCellSpec(row_match="Confirmation Number", column_index=1)
            ),
            arguments=StepArguments(output="confirmation_id", value_type=ValueType.STRING),
            preconditions=confirmed_page,
            on_error=OnError.ESCALATE,
        ),
    ]
    return _common(
        name="open_savings_subaccount",
        description="Open a savings sub-account for a member and reach the confirmation screen.",
        base_url=base_url,
        steps=steps,
        inputs={
            "member_id": InputSpec(description="Five-digit member number", pattern=r"^\d{5}$"),
            "nickname": InputSpec(description="Nickname for the new sub-account"),
            "operator_id": InputSpec(description="Operator sign-in id"),
            "access_code": InputSpec(description="Operator access code", sensitive=True),
        },
        outputs={
            "confirmation_id": OutputSpec(
                type=ValueType.STRING,
                nullable=True,
                source=ExtractSource(step_id="s12-extract-confirmation"),
            )
        },
        checkpoint=Checkpoint(
            description="Sub-account confirmation screen is shown", conditions=confirmed_page
        ),
        max_risk=RiskClass.IRREVERSIBLE,
        step_timeout_s=step_timeout_s,
    )
