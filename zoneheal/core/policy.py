import numpy as np 

class TabularSoftmaxPolicy:
    def __init__(self, n_states , n_action, seed = 0):
        self.n_states = n_states
        self.n_action = n_action

        self.theta = np.zeros((n_states, n_action), dtype = float)
        self.rng = np.random.default_rng(seed)
        self.version = 0

    
    def probablity(self,state):
        logits = self.theta[state]
        """
        How the theta table looks 
                  Left   Right
        s0         2       1
        s1         0       3
        s2        -1       2
        if state = 0;
         then ill get left , right that is the actions s0
         The logits = [2,1] -> then just take the max 
         2-2 = 0 and 1-2 = -1 , so the value remains the same meaning prob is the same 
         then take softmax  
        """
        z = logits - logits.max()
        e = np.exp(z)
        return e / e.sum()
    
    def action(self,state):
        """
        Sample the action and return the (Action , probablity)
        choose the action and rember how likely the action was 
        """

        p = self.probablity(state)
        # he below just meams based on the probalties just take the best possible outcm
        action = int(self.rng.choice(self.n_action, p = p))
        return action , float(np.log(p[action])) # we are returning the action nd the probalit that the action occired at 

    act = action

    
    def logprob(self,state,action):
        """ we are writing the log pi(a|s)"""
        return float(np.log(self.probablity(state)[action]))
    
    def grad_logprob(self,state,action):
        """
        calculates 
        ∂θs/∂logπ(a∣s)

        so what this actually does is if slightly change the theta value for this state , how does the log probality of thea ction i actually took change
        Decreasing the relative scores of the other actions increases the probability of action 1.
        """

        g = -self.probablity(state)
        g[action] += 1.0
        return g
    
    def mean_entropy(self):
        # Calcualtes how uncertai n/ random the particualr policy is across alls tates 
        # High ucnertaintiy means higher entropy 

        p = np.exp(self.theta - self.theta.max(axis=1, keepdims=True))
        p /= p.sum(axis = 1, keepdims = True)
        return float(-np.mean(np.sum(p * np.log(p), axis=1)))

    
    def snapshot(self):
        # This is the action of distribution for every state . This is the reference required for per Bucket KL 
        p = np.exp(self.theta - self.theta.max(axis = 1, keepdims = True))
        return p / p.sum(axis = 1, keepdims = True)
    
# LLm generated Test code  
def check_gradient(policy, state=0, action=2, eps=1e-6):
    """Verify grad_logprob against finite differences.
 
    If this passes, the update rule is correct and you can stop wondering
    about it. Everything downstream assumes it.
    """
    analytic = policy.grad_logprob(state, action)
    numeric = np.zeros(policy.n_action)
    for j in range(policy.n_action):
        policy.theta[state, j] += eps
        up = policy.logprob(state, action)
        policy.theta[state, j] -= 2 * eps
        down = policy.logprob(state, action)
        policy.theta[state, j] += eps          # restore
        numeric[j] = (up - down) / (2 * eps)
 
    print("analytic:", np.round(analytic, 6))
    print("numeric :", np.round(numeric, 6))
    assert np.allclose(analytic, numeric, atol=1e-5), "gradient is wrong"
    print("gradient check passed")



if __name__ == "__main__":
    pol = TabularSoftmaxPolicy(16, 4, seed=0)
    # Check at zero-init (uniform) and at a perturbed point, since a bug can
    # hide at the symmetric point where all probabilities are equal.
    check_gradient(pol)
    pol.theta = np.random.default_rng(1).normal(size=(16, 4))
    check_gradient(pol, state=5, action=1)



        
