"""The scenario's one AWS account. Inside, it is the emulator's own, so the ARNs it returns agree with CloudTrail's and
the world's without rewriting them; a scenario that declares `account` shows the agent that id instead."""

import re

ACCOUNT = "000000000000"


def _id(account: str) -> re.Pattern:
    return re.compile(rf"(?<![\w.-]){re.escape(account)}(?![\w-])")


class Shown:
    """Maps the emulator's account to the scenario's in what the agent reads, and back in what it sends."""

    def __init__(self, account: str | None = None):
        self.account = str(account or ACCOUNT)
        self._out, self._in = _id(ACCOUNT), _id(self.account)

    def out(self, text: str) -> str:
        return text if self.account == ACCOUNT else self._out.sub(self.account, text)

    def inside(self, value):
        if self.account == ACCOUNT:
            return value
        if isinstance(value, str):
            return self._in.sub(ACCOUNT, value)
        if isinstance(value, dict):
            return {k: self.inside(v) for k, v in value.items()}
        if isinstance(value, list):
            return [self.inside(v) for v in value]
        return value
