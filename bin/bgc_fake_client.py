"""Shared fake BloomGauge client for the bloomgauge_tick / lease_guard frozen tests (not a test file)."""


class Unavailable(Exception):
    pass


class FakeClient:
    Unavailable = Unavailable

    def __init__(self, manager_on=True, can_enable=True, blocker=None, events=None, down=False, fail_writes=False):
        self.manager_on, self.can_enable, self.blocker = manager_on, can_enable, blocker
        self.events, self.down, self.fail_writes = events or [], down, fail_writes
        self.reads, self.writes = [], []

    def get(self, path, params=None):
        self.reads.append(path)
        if self.down:
            raise Unavailable("BloomGauge is not running")
        if path == "/api/model-control":
            return {"currentModel": "m1", "servingModels": ["m1", "m2"], "providerControl": {"status": "running"}}
        if path == "/api/optimizer/control":
            auto = {"mode": "auto" if self.manager_on else "manual", "canEnable": self.can_enable}
            if self.blocker:
                auto["blocker"] = {"code": self.blocker}
            return {"automatic": auto, "operation": {"status": "active"}}
        if path == "/api/agent-access":
            return {"enabled": True, "keyPath": "/k/agent.key"}
        if path == "/api/activity/log":
            return {"events": list(self.events)}
        raise AssertionError("unexpected path " + path)

    def set_manager(self, args):
        self.writes.append(("set_manager", args))
        if self.fail_writes:
            raise Unavailable("TURN_ON")
        self.manager_on = bool(args.get("on"))

    def pick_model(self, args):
        self.writes.append(("pick_model", args))
