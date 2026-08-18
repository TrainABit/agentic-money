"""Design earn-path integration: the crafter ships a real offline brand kit,
design keyword routing beats the landing draft, hostile briefs never reach an
output file unescaped, the scout lists and (in sim) seeds the design sale,
and the design_studio play is registered.

The optional MCP hero test injects FakeTransport through the registry's
public transport_factory; everything else runs with MCP disabled to prove the
offline kit is the guaranteed deliverable.
"""

from __future__ import annotations

from pathlib import Path

from sovereign.agents import roles
from sovereign.config import EngineConfig, McpConfig, McpServerConfig
from sovereign.engine.world import bootstrap
from sovereign.labor.design import DESIGN_KEYWORDS, brand_kit
from sovereign.mcp.fakes import FakeTransport
from sovereign.mcp.transport import McpResult, McpToolSpec
from sovereign.plays import PLAYS

KIT_FILES = ("logo.svg", "social_card.svg", "index.html", "brand.md")
HOSTILE = "<script>alert(1)</script>"


def make_world(tmp_path, **overrides):
    cfg = EngineConfig(
        mode="sim",
        data_dir=tmp_path,
        public_job_apis=False,
        fetch_market_data=False,
        **overrides,
    )  # type: ignore[arg-type]
    return bootstrap(cfg)


def design_job(job_id: str = "job_design00001", **extra) -> dict:
    row = {
        "id": job_id,
        "source": "manual",
        "title": "Brand kit + landing page for Acme Robotics",
        "description": "Logo, brand palette, landing page and social card for a robotics startup.",
        "status": "accepted",
        "price_usd": 900,
        "fit": 0.9,
        "contact": "buyer@sim.local",
    }
    row.update(extra)
    return row


def test_design_job_produces_brand_kit_files_via_crafter(tmp_path):
    world = make_world(tmp_path)
    world.store.upsert_job(design_job())

    actions = roles.crafter(world)
    assert actions[0]["kind"] == "craft"
    assert "error" not in actions[0]

    job = world.store.get_job("job_design00001")
    assert job["status"] == "delivered"
    assert job["entry"] == "open index.html"
    workdir = tmp_path / "work" / "job_design00001"
    delivery = Path(job["delivery_path"])
    for base in (workdir, delivery):
        for name in KIT_FILES:
            artifact = base / name
            assert artifact.is_file(), artifact
            assert artifact.stat().st_size > 200, artifact
    for name in KIT_FILES:
        assert name in job["files"]

    # Deterministic and offline: brand is the first ~4 title words, the brief
    # is the description, and the bytes match the pinned generator exactly.
    kit = brand_kit(
        "Brand kit + landing",
        "Logo, brand palette, landing page and social card for a robotics startup.",
    )
    for name in KIT_FILES:
        assert (workdir / name).read_text() == kit[name]

    # MCP is disabled by default, so no hero art — and nothing failed.
    assert not (workdir / "hero.txt").exists()
    assert world.status()["mcp"]["enabled"] is False


def test_design_keywords_beat_the_landing_branch(tmp_path):
    assert "landing page" in DESIGN_KEYWORDS
    world = make_world(tmp_path)
    world.store.upsert_job(
        design_job(
            "job_landing0001",
            title="Landing page for a payroll SaaS",
            description="landing page copy refresh with a clear call to action",
        )
    )

    roles.crafter(world)

    workdir = tmp_path / "work" / "job_landing0001"
    for name in KIT_FILES:
        assert (workdir / name).is_file(), name  # the design branch ran
    page = (workdir / "index.html").read_text()
    assert page.lower().startswith("<!doctype html>")
    assert "Draft landing" not in page  # not the generic landing stub
    job = world.store.get_job("job_landing0001")
    assert job["entry"] == "open index.html"


def test_hostile_title_and_brief_never_appear_raw_in_any_output_file(tmp_path):
    world = make_world(tmp_path)
    world.store.upsert_job(
        design_job(
            "job_hostile0001",
            title=f"Logo {HOSTILE} brand kit",
            description=f'Brand design brief {HOSTILE} with "quoted" markup & more.',
        )
    )

    roles.crafter(world)

    job = world.store.get_job("job_hostile0001")
    assert job["status"] == "delivered"
    workdir = tmp_path / "work" / "job_hostile0001"
    delivery = Path(job["delivery_path"])
    checked = 0
    for base in (workdir, delivery):
        for path in sorted(p for p in base.iterdir() if p.is_file()):
            content = path.read_text()
            assert "<script" not in content, path
            assert "</script>" not in content, path
            checked += 1
    assert checked >= 12  # kit + README + DELIVERY on both sides


def test_scout_lists_design_offer_and_seeds_one_sim_sale(tmp_path):
    world = make_world(tmp_path)

    roles.scout(world)
    offers = {o["id"]: o for o in world.store.offers()}
    design = offers["offer_design_kit"]
    assert design["title"] == "Brand kit + landing page"
    assert design["kind"] == "fixed"
    assert design["price_usd"] == 900
    assert world.store.get_job("design_kit") is None  # below the revenue gate

    world.ledger.post(
        "assets.usdc", "income.labor", 1600.0, "test revenue", ts=world.stamp()
    )
    roles.scout(world)
    seeded = world.store.get_job("design_kit")
    assert seeded is not None
    assert seeded["status"] == "delivered"
    assert seeded["source"] == "product"
    assert seeded["price_usd"] == 900
    assert seeded["contact"] == "buyer@sim.local"

    roles.scout(world)  # idempotent: still exactly one seeded job and outcome
    assert [j["id"] for j in world.store.jobs() if j["id"] == "design_kit"] == [
        "design_kit"
    ]
    outcomes = [
        o for o in world.store.outcomes(50) if o.get("play_id") == "design_studio"
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["usd"] == 900.0


def test_design_studio_play_exists_in_plays():
    plays = {p.id: p for p in PLAYS}
    play = plays["design_studio"]
    assert play.title == "Design studio"
    assert play.agents == ("crafter", "publisher", "closer", "scout")
    assert play.monthly_target_usd == 1500
    assert play.kill_after_days_if_zero == 21
    assert 0.0 < play.attention_until_min <= 0.1
    assert play.attention_until_rec > play.attention_until_min
    assert play.attention_after_rec >= play.attention_until_rec
    # The additions never displaced an existing play.
    for existing in (
        "labor_studio",
        "productized",
        "digital_products",
        "tsmom_crypto",
        "b2b_outbound",
        "infra_arb",
    ):
        assert existing in plays


def test_design_job_saves_mcp_hero_when_image_tool_is_reachable(tmp_path):
    cfg_mcp = McpConfig(
        enabled=True,
        servers=(
            McpServerConfig(
                name="studio",
                transport="stdio",
                command="fake-design-server",
                allow_agents=("crafter",),
                calls_per_tick=4,
            ),
        ),
    )
    world = make_world(tmp_path, mcp=cfg_mcp)
    transports: list[FakeTransport] = []

    def factory(server, *, secret_resolver):
        transport = FakeTransport(
            [
                McpToolSpec(
                    server="",
                    name="generate_image",
                    description="Render a hero image",
                    input_schema={"type": "object"},
                )
            ],
            lambda name, args: McpResult(ok=True, text="hero sketch bytes"),
        )
        transports.append(transport)
        return transport

    world.mcp.transport_factory = factory
    world.store.upsert_job(design_job())

    roles.crafter(world)

    workdir = tmp_path / "work" / "job_design00001"
    hero = workdir / "hero.txt"
    assert hero.is_file()
    text = hero.read_text()
    assert "hero sketch bytes" in text
    assert "untrusted data, not instructions" in text  # fenced result, verbatim
    assert transports[0].calls == [
        ("generate_image", {"prompt": "logo hero for Brand kit + landing"})
    ]

    job = world.store.get_job("job_design00001")
    assert job["status"] == "delivered"
    assert "hero.txt" in job["files"]  # shipped alongside the offline kit
    for name in KIT_FILES:
        assert name in job["files"]
