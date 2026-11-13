from types import SimpleNamespace

from awsbench_aws.actionlog import ActionLog
from awsbench_aws.emulator import Emulator
from awsbench_aws.ports import AwsPort


def port() -> AwsPort:
    p = AwsPort(Emulator(home="/nonexistent"), ActionLog())
    p.client = lambda service, region="us-east-1": SimpleNamespace(put_parameter=lambda **kw: {})
    return p


def test_writes_made_for_an_agent_call_are_logged_under_it():
    p = port()
    p.acting = {"call_no": 16, "role": "sre-primary"}
    p.call("ssm", "PutParameter", {"Name": "/x", "Value": "1"}, principal="sre-primary", world_made=False)
    p.call("ssm", "PutParameter", {"Name": "/y", "Value": "1"}, principal="world:physics", world_made=True)
    agent, world = p.actionlog.export()
    assert (agent["call_no"], agent["role"]) == (16, "sre-primary")
    assert (world["call_no"], world["role"]) == (None, "world")
