"""Casts composition — roles, modes, profiles, and packs.

lionagi's casts system defines agent identity through composable patterns:
- **Role** — what an agent does (auditor, critic, architect, …)
- **Mode** — how it reasons (adversarial, evidential, fast, …)
- **Profile** — Role + Modes, with conflict validation
- **Pack** — per-role policy and runtime configuration overlays
- **AgentSpec** — Profile + model + tools + permissions → ready-to-build

No LLM calls required — runs instantly.

    uv run python examples/casts_composition.py
"""

from __future__ import annotations

import asyncio
import importlib.resources

from lionagi.agent import AgentSpec, create_agent
from lionagi.casts.emission import Finding, SpawnRequest, Verdict, build_emission_operable
from lionagi.casts.pack import Pack
from lionagi.casts.pattern import Mode, Role, list_modes, list_roles
from lionagi.casts.profile import Profile


async def main():
    # ── Available roles and modes ────────────────────────────────────────
    roles = sorted(list_roles())
    modes = sorted(list_modes())
    print(f"{len(roles)} roles: {', '.join(roles[:8])}, …")
    print(f"{len(modes)} modes: {', '.join(modes)}")

    # ── Load and inspect a role ──────────────────────────────────────────
    critic = Role.load("critic")
    print("\ncritic role:")
    print(f"  emits: {[e.__name__ for e in critic.emits]}")
    print(f"  body: {len(critic.body)} chars")

    # ── Mode conflict detection ──────────────────────────────────────────
    fast = Mode.load("fast")
    Mode.load("slow")  # loaded to confirm it exists; conflict tested below
    print(f"\nfast conflicts_with: {fast.conflicts_with}")

    try:
        Profile.compose("critic", modes=["fast", "slow"])
        print("  ERROR: fast+slow should conflict")
    except ValueError:
        print("  fast+slow: conflict detected (correct)")

    # fast + adversarial is fine
    Profile.compose("critic", modes=["fast", "adversarial"])
    print("  fast+adversarial: OK")

    # ── Profile → system message ─────────────────────────────────────────
    profile = Profile.compose("auditor", modes=["evidential", "systematic"])
    sys_msg = profile.build_system_message()
    print(f"\nauditor+evidential+systematic: {len(sys_msg)} char system message")

    # ── Emission operable (structured output contract) ───────────────────
    em = profile.emission_operable()
    print(f"  emission operable: {em.name if em else 'none'}")

    custom = build_emission_operable((Finding, Verdict, SpawnRequest), name="custom_audit")
    print(f"  custom emission: {custom.name}")

    # ── Pack (per-role config overlays) ──────────────────────────────────
    pack_path = importlib.resources.files("lionagi.casts.packs") / "default.yaml"
    pack = Pack.from_file(str(pack_path))
    print(f"\nPack '{pack.name}': {len(pack.configs)} role configs, {len(pack.policies)} policies")
    if cfg := pack.config("critic"):
        print(f"  critic: active={cfg.active}, default_modes={cfg.default_modes}")
    if pol := pack.policy("critic"):
        print(f"  critic authority: {pol.authority[0][:60]}…")

    # ── AgentSpec → Branch ───────────────────────────────────────────────
    spec = AgentSpec.compose("reviewer", modes=["adversarial"])
    branch = await create_agent(spec, load_settings=False)
    sys = branch.msgs.system
    content = str(sys.content) if sys and hasattr(sys, "content") else ""
    print(f"\nAgentSpec → Branch {str(branch.id)[:8]}: {len(content)} char system prompt")

    # ── All roles compose cleanly ────────────────────────────────────────
    errors = []
    for name in roles:
        try:
            Profile.compose(name)
        except Exception as e:
            errors.append((name, str(e)))

    print(f"\nFull roster: {len(roles) - len(errors)}/{len(roles)} roles compose cleanly")
    for name, err in errors:
        print(f"  FAIL: {name}: {err}")

    print("\nAll checks passed." if not errors else "")


if __name__ == "__main__":
    asyncio.run(main())
