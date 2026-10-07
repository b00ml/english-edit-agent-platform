"""Guard the public lockfile and audit gate which caused the frontend CI failure."""

import json
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.lstrip("^~").split(".")[:3])


def test_frontend_lockfile_removes_reported_vulnerable_versions() -> None:
    manifest = json.loads((ROOT / "frontend/package.json").read_text(encoding="utf-8"))
    lock = json.loads((ROOT / "frontend/package-lock.json").read_text(encoding="utf-8"))
    assert _version(manifest["dependencies"]["axios"]) >= (1, 20, 0)
    assert lock["packages"][""]["dependencies"]["axios"] == manifest["dependencies"]["axios"]
    assert _version(lock["packages"]["node_modules/axios"]["version"]) >= (1, 20, 0)
    assert _version(lock["packages"]["node_modules/source-map-js"]["version"]) >= (1, 2, 2)
    assert (
        "source-map-js" not in manifest["dependencies"]
    )  # Transitive fix, no unused runtime dependency.


def test_frontend_ci_keeps_clean_install_audit_and_build_gates() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/backend-ci.yml").read_text(encoding="utf-8")
    )
    steps = workflow["jobs"]["frontend"]["steps"]
    commands = [step.get("run") for step in steps if "run" in step]
    assert commands == ["npm ci", "npm audit --audit-level=moderate", "npm run build"]
    audit = next(step for step in steps if step.get("run", "").startswith("npm audit"))
    assert audit["working-directory"] == "frontend"
    assert not audit.get("continue-on-error", False)
    assert not workflow["jobs"]["frontend"].get("continue-on-error", False)


def test_official_setup_actions_use_verified_node24_major_versions() -> None:
    workflow = yaml.safe_load(
        (ROOT / ".github/workflows/backend-ci.yml").read_text(encoding="utf-8")
    )
    expected = {"actions/checkout", "actions/setup-python", "actions/setup-node"}
    seen: set[str] = set()
    for job in workflow["jobs"].values():
        for step in job["steps"]:
            action = step.get("uses", "")
            if action.split("@")[0] in expected:
                repository, version = action.split("@")
                assert version == "v6"
                seen.add(repository)
                if repository == "actions/setup-node":
                    assert step["with"]["node-version"] == "22"
    assert seen == expected
