class Server_Weighted_Round_Robin:
    name = "Weighted_Agent"

    def reset(self):
        pass

    def act(self,state,feat):
        servers = feat["server"]
        total = sum(servers.values())
        weights = {}
        for z in servers:
            weights[z] = servers[z] / total
        return weights # returna  dictonary 
    
    def observe(self,state,action,reward,info):
        pass