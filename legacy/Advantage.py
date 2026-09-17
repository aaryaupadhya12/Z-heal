import numpy as np
 
from rollout import episode_return
 
 
def naive(traj):
    """
    1. Naive every steps gets the episodes total return 
    ocirrect in expectation but very noisty a bad step in a succesfull episode gets rewarded anywas
    Groups exisit to reuce this nouse in Gigpo , GRPO etc 
    W want to make sure that episodes that return G = 0 , to habe 0 influce on teh gradients 

    """
    return np.full(len(traj), episode_return(traj), dtype=float)
    # So if the R of the step= 0 returnrs traj = [0,0,0,o] or else if it was 1 return [1,1,1,1]

def discounted_to_go(traj, gamma=0.99):
    """
    This is the easiest way into thinlong about future credit assigments , where the future rewards are seen adn 
    they are disciunted via the 
    """
    advantage = np.zeros(len(traj), dtype=float)
    running = 0.0
    for t in reversed(range(len(traj))):
        running = traj[t].reward + gamma * running
        advantage[t] = running
    return advantage

class WithBaseline:
    def __init__(self, fn , momentum = 0.99):
        self.fn = fn 
        self.momentum = momentum
        self.b = 0
        self._seen = False


    def __call__(self, traj):
        raw = self.fn(traj)
        advantage = raw - self.b

        R = episode_return(traj)
        if not self._seen:
            self.b = R 
            self._seen = True
        else:
            self.b = self.momentum * self.b + (1 - self.momentum) * R
        return advantage
    
    def __repr__(self):
        return f"WithBaseline({self.fn.__name__}, b={self.b:.4f})"


def bandit(traj):
    # We know that bandit action -> state -> reward 
    # Expect only one step , if more than that not going to rpoceess (simple bandot operation)

    assert len(traj) == 1
    
    return np.array([traj[0].reward], dtype=float)
    # We only retiurn 1 advatage per trajectory step 


if __name__ == "__main__":
    from rollout import Step
 
    # A 4-step episode that succeeds only at the end.
    traj = [Step(obs=i, action=0, reward=0.0, logprob=0.0, policy_version=0)
            for i in range(3)]
    traj.append(Step(obs=3, action=0, reward=1.0, logprob=0.0, policy_version=0))
 
    print("naive           :", naive(traj))
    print("discounted 0.99 :", np.round(discounted_to_go(traj, 0.99), 4))
    print("discounted 0.5  :", np.round(discounted_to_go(traj, 0.5), 4))
 
    wb = WithBaseline(naive)
    print("\nbaseline warming up over repeated successes:")
    for i in range(5):
        print(f"  call {i}: adv={np.round(wb(traj), 4)}  b={wb.b:.4f}")



            
