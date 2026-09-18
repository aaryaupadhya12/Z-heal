from .envoy_zone_aware import aws_defaults, routing_fractions

class Envoy:
    """AWS zone-aware routing: a port of Envoy's algorithm. No learning."""
    name = "aws_envoy"

    def __init__(self, n_zones=4):
        self.cfg = aws_defaults(n_zones)      # minimum endpoints = 2 x zones

    def reset(self):
        pass

    def act(self, state, feats):
        zones = sorted(feats["servers"])
        servers = {z: round(feats["servers"][z]) for z in zones}
        clients = feats["clients"]
        row, _ = routing_fractions(feats["zone"], zones, clients, clients,
                                   servers, servers, self.cfg)
        return row

    def observe(self, state, action, reward, info):
        pass