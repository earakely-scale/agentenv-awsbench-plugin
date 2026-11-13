from types import SimpleNamespace

from awsbench_aws.server import NO_SCENARIO, AwsServer

SRE, HARNESS = SimpleNamespace(role="sre-primary"), SimpleNamespace(role="harness")


async def test_charged_calls_without_a_scenario_are_refused_uncharged():
    server = AwsServer()

    async def work(call):
        raise AssertionError("ran without a scenario")

    out = await server._metered(SRE, "aws_cli", {"command": "aws route53 list-hosted-zones"}, work)
    assert NO_SCENARIO in out and server.meter.calls_used == 0
    assert NO_SCENARIO in await server.wait(SRE, minutes=5)
    assert server.meter.calls_used == 0


async def test_uncharged_roles_still_run_without_a_scenario():
    server = AwsServer()

    async def work(call):
        return {"ran": True}

    server.ready.set()
    assert '"ran":true' in await server._metered(HARNESS, "fleet_list_hosts", {}, work)


def test_wait_minutes_are_clamped_before_the_date_math():
    server = AwsServer()
    now = server.meter.now
    assert (server._wait_target(1e308, None) - now).total_seconds() == server.meter.cfg.wait_max_s
