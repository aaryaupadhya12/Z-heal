# Force local is same as how its on envoy where the spilling is not seperated between endpoints instead we just see how they fail and see latency and utilization increase across reach node and get metrics 
class ForceLocal:
    name = "forced_local"

    def reset(self):
        pass 
        
    def act(self,state,*feats):
        return 0
    
    def observe(self, state, action, reward, info):
        pass