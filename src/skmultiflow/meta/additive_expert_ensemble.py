import copy as cp
import numpy as np
from skmultiflow.core.base import StreamModel
from skmultiflow.bayes import NaiveBayes


class AdditiveExpertEnsemble(StreamModel):
    """
    Additive Expert Ensemble [1]_.

    Parameters
    ----------
    n_estimators: int (default=5)
        Maximum number of estimators to hold.
    base_estimator: StreamModel or sklearn.BaseEstimator (default=NaiveBayes)
        Each member of the ensemble is an instance of the base estimator.
    beta: float (default=0.8)
        Factor for which to decrease weights by.
    gamma: float (default=0.1)
        Weight of new experts in ratio to total ensemble weight.
    pruning: 'oldest' or 'weakest' (default='weakest')
        Pruning strategy to use.

    Notes
    -----
    Additive Expert Ensemble (AddExp) is a general method for using any online
    learner for drifting concepts. Using the 'oldest' pruning strategy leads to
    known mistake and error bounds, but using 'weakest' is generally better
    performing.

    Bound on mistakes when using 'oldest' pruning strategy (theorem 3.1 from
    [1]_):
    Let Wi denote the total weight of the ensemble at time step i, and Mi the
    number of mistakes of the ensemble at all time steps up to i-1; then for
    any time step t1 < t2, and if we stipulate that beta + 2 * gamma < 1:
        M2 - M1 <= log(W1 - W2) / log(2 / (1 + beta + 2 * gamma))

    References
    __________
    .. [1] Kolter and Maloof. Using additive expert ensembles to cope with
        Concept drift. Proc. 22 International Conference on Machine Learning,
        2005.
    """

    class WeightedExpert:
        """
        Wrapper that includes an estimator and its weight.

        Parameters
        ----------
        estimator: StreamModel or sklearn.BaseEstimator
            The estimator to wrap.
        weight: float
            The estimator's weight.
        """
        def __init__(self, estimator, weight):
            self.estimator = estimator
            self.weight = weight

    def __init__(self, n_estimators=5, base_estimator=NaiveBayes(), beta=0.8,
                 gamma=0.1, pruning='weakest'):
        """
        Creates a new instance of AdditiveExpertEnsemble.
        """
        super().__init__()

        self.max_experts = n_estimators
        self.base_estimator = base_estimator

        self.beta = beta
        self.gamma = gamma
        self.pruning = pruning
        assert self.pruning in ('weakest', 'oldest'), \
            'Unknown pruning strategy: {}'.format(self.pruning)

        # Following attributes are set later
        self.epochs = None
        self.num_classes = None
        self.experts = None

        self.reset()

    def fit(self, X, y, classes=None, weight=None):
        raise NotImplementedError

    def partial_fit(self, X, y, classes=None, weight=None):
        """
        Partially fits the model on the supplied X and y matrices.

        Since it's an ensemble learner, if X and y matrix of more than one
        sample are passed, the algorithm will partial fit the model one sample
        at a time.

        Parameters
        ----------
        X: Numpy.ndarray of shape (n_samples, n_features)
            Features matrix used for partially updating the model.

        y: Array-like
            An array-like of all the class labels for the samples in X.

        classes: list
            List of all existing classes. This is an optional parameter, except
            for the first partial_fit call, when it becomes obligatory.

        weight: None
            Instance weight. This is ignored by the ensemble and is only
            for compliance with the general skmultiflow interface.

        Returns
        -------
        DynamicWeightedMajority
            self
        """
        for i in range(len(X)):
            self.fit_single_sample(
                X[i:i+1, :], y[i:i+1], classes, weight
            )
        return self

    def predict(self, X):
        """ predict

        The predict function will take an average of the precitions of its
        learners, weighted by their respective weights, and return the most
        likely class.

        Parameters
        ----------
        X: Numpy.ndarray of shape (n_samples, n_features)
            A matrix of the samples we want to predict.

        Returns
        -------
        numpy.ndarray
            A numpy.ndarray with the label prediction for all the samples in X.
        """
        preds = np.array([np.array(exp.estimator.predict(X)) * exp.weight
                          for exp in self.experts])
        sum_weights = sum(exp.weight for exp in self.experts)
        aggregate = np.sum(preds / sum_weights, axis=0)
        return (aggregate + 0.5).astype(int)    # Round to nearest int

    def predict_proba(self, X):
        raise NotImplementedError

    def fit_single_sample(self, X, y, classes=None, weight=None):
        """
        Predict + update weights + modify experts + train on new sample.
        (As was originally described by [1])
        """
        self.epochs += 1
        self.num_classes = max(
            len(classes) if classes is not None else 0,
            (int(np.max(y)) + 1), self.num_classes)

        # Get expert predictions and aggregate in y_hat
        predictions = np.zeros((self.num_classes,))
        for exp in self.experts:
            y_hat = exp.estimator.predict(X)
            predictions[y_hat] += exp.weight
            if np.any(y_hat != y):
                exp.weight *= self.beta

        # Output prediction
        y_hat = np.array([np.argmax(predictions)])

        # Update expert weights
        if self.pruning == 'weakest':
            self.experts = sorted(self.experts, key=lambda exp: exp.weight)

        # If y_hat != y_true, then add a new expert
        if np.any(y_hat != y):
            ensemble_weight = sum(exp.weight for exp in self.experts)
            new_exp = self._construct_new_expert(ensemble_weight * self.gamma)
            self._add_expert(new_exp)

        # Pruning to self.max_experts if needed
        if len(self.experts) > self.max_experts:
            self.experts.pop(0)

        # Train each expert on X
        for exp in self.experts:
            exp.estimator.partial_fit(X, y, classes=classes, weight=weight)

        # Normalize weights (if not will tend to infinity)
        if self.epochs % 1000:
            ensemble_weight = sum(exp.weight for exp in self.experts)
            for exp in self.experts:
                exp.weight /= ensemble_weight

    def get_expert_predictions(self, X):
        """
        Returns predictions of each class for each expert.
        In shape: (n_experts,)
        """
        return [exp.estimator.predict(X) for exp in self.experts]

    def _add_expert(self, new_exp):
        """
        Inserts the new expert on the sorted self.experts list.
        """
        if self.pruning == 'oldest':
            self.experts.append(new_exp)
        elif self.pruning == 'weakest':
            idx = 0
            for exp in self.experts:
                if exp.weight < new_exp.weight:
                    break
                idx += 1
            self.experts.insert(idx, new_exp)

    def _construct_new_expert(self, weight=1):
        """
        Constructs a new WeightedExpert from the provided base_estimator.
        """
        return self.WeightedExpert(cp.deepcopy(self.base_estimator), weight)

    def reset(self):
        self.epochs = 0
        self.num_classes = 2
        self.experts = [
            self._construct_new_expert()
        ]

    def score(self, X, y):
        raise NotImplementedError

    def get_info(self):
        return \
            type(self).__name__ + ': ' + \
            "max_estimators: {} - ".format(self.max_experts) + \
            "base_estimator: {} - ".format(self.base_estimator.get_info()) + \
            "beta: {} - ".format(self.beta) + \
            "gamma: {}".format(self.gamma)
