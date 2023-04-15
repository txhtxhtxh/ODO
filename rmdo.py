import os
import gc
import sys
import time
import pyspiel
import argparse
import numpy as np
from copy import deepcopy
import blotto
import large_kuhn_poker

from dependencies.open_spiel.open_spiel.python import policy
from dependencies.open_spiel.open_spiel.python.algorithms import cfr
from dependencies.open_spiel.open_spiel.python.algorithms import best_response
from dependencies.open_spiel.open_spiel.python.algorithms import exploitability
from dependencies.open_spiel.open_spiel.python.algorithms import outcome_sampling_mccfr as outcome_mccfr

module_path = os.path.abspath(os.path.join(''))
if module_path not in sys.path:
    sys.path.append(module_path)


def ensure_dir(file_path):
    directory = os.path.dirname(file_path)
    if not os.path.exists(directory):
        os.makedirs(directory, exist_ok=True)


class WrappedOSMCCFRSolver(outcome_mccfr.OutcomeSamplingSolver):
    def __init__(self, game):
        super().__init__(game)

    def evaluate_and_update_policy(self):
        self.iteration()


class MetaState:
    def __init__(self, game, state, brs, br_id=None):
        self.state = state
        self.brs = brs
        self.br_id = br_id
        self.game = game

    def legal_actions(self, player=None):
        if player is not None and player != self.state.current_player():
            # assumption
            return []
        if self.state.history_str() in self.game._legal_actions_cache:
            return self.game._legal_actions_cache[self.state.history_str()]
        if self.brs == 'empty' and self.state.current_player() == self.br_id:
            return self.state.legal_actions()
        if (self.br_id is None and self.state.current_player() in [0, 1]) or \
                (self.br_id is not None and self.state.current_player() == self.br_id):
            legal_actions = set()
            for br in self.brs[self.state.current_player()]:
                legal_actions.add(br.best_response_action(self.state.information_state_string()))
            ans = list(legal_actions)
            self.game._legal_actions_cache[self.state.history_str()] = ans
            return ans
        else:
            return self.state.legal_actions()

    def child(self, a):
        return MetaState(self.game, self.state.child(a), self.brs, br_id=self.br_id)

    def __getattr__(self, attr):
        # hacky hacky hacky hacky
        assert attr != 'new_initial_state'
        return self.state.__getattribute__(attr)

    def clone(self):
        return MetaState(self.game, self.state.clone(), self.brs, br_id=self.br_id)


class MetaGame:
    def __init__(self, game, brs, br_id=None):
        self.alter_state = pyspiel.load_game("kuhn_poker").new_initial_state
        self.game = game
        self.get_type = game.get_type
        self.num_players = game.num_players

        self.brs = brs
        self.br_id = br_id
        self._legal_actions_cache = dict()

    def new_initial_state(self):
        return MetaState(self, self.game.new_initial_state(), self.brs, br_id=self.br_id)

    def __getattr__(self, attr):
        assert attr != 'new_initial_state'
        return self.game.__getattribute__(attr)


class ExpandTabularPolicy:
    def __init__(self, p):
        tabular_p = p.to_tabular()
        self.state_lookup = deepcopy(tabular_p.state_lookup)
        self.action_probability_array = deepcopy(tabular_p.action_probability_array)

    def action_probabilities(self, state):
        state_str = state.information_state_string()
        if state_str in self.state_lookup:
            probability = self.action_probability_array[self.state_lookup[state_str]]
            return {action: probability[action] for action in state.legal_actions()}
        return {a: 1 / len(state.legal_actions()) for a in state.legal_actions()}


def merge_two_policies(previous_policy, current_policy, iter, prev_iter):
    """Merges two policies(one is the other's subset) into single joint policy for fixed player.

    Missing states are filled with a valid uniform policy.

    Args:
      current_policy: avg policy of current window
      previous_policy: avg policy of previous windows
      game: The game corresponding to the resulting TabularPolicy.
      iter: Useful in uniform average


    Returns:
      merged_policy: A TabularPolicy with each player i's policy taken from the
        ith joint_policy.
    """
    merged_policy = current_policy
    for p_state in current_policy.state_lookup.keys():
        to_index = merged_policy.state_lookup[p_state]
        # Only copy if the state exists, otherwise fall back onto uniform.
        current_prob_array = current_policy.action_probability_array[current_policy.state_lookup[p_state]]
        if p_state in previous_policy.state_lookup:
            previous_prob_array = previous_policy.action_probability_array[
                previous_policy.state_lookup[p_state]]
            merged_policy.action_probability_array[to_index] = (previous_prob_array * prev_iter + current_prob_array * (
                    iter - prev_iter)) / iter
        else:
            merged_policy.action_probability_array[to_index] = current_prob_array
        merged_policy.action_probability_array[to_index] /= np.sum(
            merged_policy.action_probability_array[to_index])
    return merged_policy


def merge_policies(policies, windows, cur_it, gap=1):
    """Merges two policies(one is the other's subset) into single joint policy for fixed player.

    Missing states are filled with a valid uniform policy.

    Args:
      current_policy: avg policy of current window
      previous_policy: avg policy of previous windows
      game: The game corresponding to the resulting TabularPolicy.
      iter: Useful in uniform average


    Returns:
      merged_policy: A TabularPolicy with each player i's policy taken from the
        ith joint_policy.
    """
    merged_policy = policies[-1]
    for p_state in policies[-1].state_lookup.keys():
        to_index = merged_policy.state_lookup[p_state]
        merged_policy.action_probability_array[to_index] *= ((cur_it - windows[-1]) * gap)

        # Only copy if the state exists, otherwise fall back onto uniform.
        for i, tabular_policy in enumerate(policies[:-1]):
            if p_state in tabular_policy.state_lookup:
                tabular_policy_array = tabular_policy.action_probability_array[
                    tabular_policy.state_lookup[p_state]]
                merged_policy.action_probability_array[to_index] += (
                        gap * (windows[i + 1] - windows[i]) * tabular_policy_array)
            else:
                merged_policy.action_probability_array[to_index] += 0
        merged_policy.action_probability_array[to_index] /= np.sum(
            merged_policy.action_probability_array[to_index])

    return merged_policy


class XODO:
    def __init__(self, algorithm: str, game_name: str, meta_iterations: int, data_collect_frequency: int,
                 meta_solver: str):
        self.algorithm = algorithm
        self.game_name = game_name
        self.meta_solver = meta_solver
        self.meta_iterations = meta_iterations
        self.data_collect_frequency = data_collect_frequency

    def reset_game(self):
        # Set up game environment
        if self.game_name == "oshi_zumo":
            COINS = 4
            SIZE = 1
            HORIZON = 6
            game = pyspiel.load_game(self.game_name,
                                     {
                                         "coins": pyspiel.GameParameter(COINS),
                                         "size": pyspiel.GameParameter(SIZE),
                                         "horizon": pyspiel.GameParameter(HORIZON)
                                     })
            game = pyspiel.convert_to_turn_based(game)
        elif game_name == "goofspiel":
            game = pyspiel.load_game(game_name, {"players": pyspiel.GameParameter(2)})
            game = pyspiel.convert_to_turn_based(game)
        elif game_name == "phantom_ttt":
            game = pyspiel.load_game(game_name)
        elif game_name == "blotto":
            game = blotto.BlottoGame()
        elif game_name == "python_large_kuhn_poker":
            game = large_kuhn_poker.KuhnPokerGame()
        else:
            game = pyspiel.load_game(self.game_name)
        return game

    def reset_meta_solver(self, restricted_game):
        if self.meta_solver == 'cfr_plus':
            meta_solver = cfr.CFRPlusSolver(restricted_game)
        elif self.meta_solver == 'cfr':
            meta_solver = cfr.CFRSolver(restricted_game)
        else:
            raise ValueError("Algorithm unidentified")
        return meta_solver

    def run(self, game, iterations, seed):
        brs = []
        k = 0
        br_actions = {}
        xodo_times = []
        xodo_exps = []
        xodo_infostates = []
        num_infostates = 0
        start_time = time.time()
        previous_avg_policy, current_window_policy, prev_iter = None, None, None

        # Compute BR
        uniform = policy.UniformRandomPolicy(game)
        for pid in range(2):
            br = best_response.BestResponsePolicy(game, pid, uniform)
            br.expanded_infostates = 0
            root_state = game.new_initial_state()
            _ = br.value(root_state)
            for key, action in br.cache_best_response_action.items():
                br_actions[key] = [action]
            brs.append(br)
            num_infostates += br.expanded_infostates
        new_br = True
        br_list = [[brs[0]], [brs[1]]]

        # Construct meta game
        restricted_game = MetaGame(game, br_list)
        meta_solver = self.reset_meta_solver(restricted_game)
        current_window_policy = ExpandTabularPolicy(meta_solver.average_policy())

        for i in range(iterations):
            if (self.algorithm == "XODO") and previous_avg_policy:
                avg_policy = merge_two_policies(previous_avg_policy, current_window_policy, i, prev_iter)
            else:
                avg_policy = current_window_policy
            conv = exploitability.exploitability(game, avg_policy)
            save_prefix = './results/' + self.algorithm + str(self.meta_iterations) + '_' + self.game_name + f'_{seed}'

            if (new_br and i > 0) or i % self.data_collect_frequency == 0:
                print("Iteration {} exploitability {}".format(i, conv))
                wall_time = time.time() - start_time
                xodo_times.append(wall_time)
                xodo_exps.append(conv)
                xodo_infostates.append(num_infostates)
                ensure_dir(save_prefix)
                if time.time() - start_time < 258000:
                    np.save(save_prefix + '_times', np.array(xodo_times))
                    np.save(save_prefix + '_exps', np.array(xodo_exps))
                    np.save(save_prefix + '_infostates', np.array(xodo_infostates))

            # If there is new BR, construct meta-game, increase window count and reset strategy
            if new_br:
                k += 1
                np.save(save_prefix + '_k', np.array(k, xodo_exps[-1]))

            if new_br and i > 0:
                restricted_game = MetaGame(game, br_list)
                meta_solver = self.reset_meta_solver(restricted_game)
                if previous_avg_policy:
                    del previous_avg_policy
                prev_iter = i
                previous_avg_policy = avg_policy

            # Run meta-strategy updates
            meta_solver.num_infostates_expanded = 0
            for _ in range(self.meta_iterations):
                meta_solver.evaluate_and_update_policy()
            num_infostates += meta_solver.num_infostates_expanded

            # Compute BR
            new_brs = []
            new_br = False
            current_window_policy = ExpandTabularPolicy(meta_solver.average_policy())
            for pid in range(2):
                br = best_response.BestResponsePolicy(game, pid, current_window_policy)
                br.expanded_infostates = 0
                _ = br.value(game.new_initial_state())
                num_infostates += br.expanded_infostates
                # Get the best response action for unvisited states
                for infostate in set(br.infosets) - set(br.cache_best_response_action):
                    br.best_response_action(infostate)
                for key, action in br.cache_best_response_action.items():
                    if key in br_actions:
                        if action not in br_actions[key]:
                            br_actions[key].append(action)
                            new_br = True
                    else:
                        br_actions[key] = [action]
                        new_br = True
                new_brs.append(br)
            if new_br:
                for pid in [0, 1]:
                    br_list[pid].append(new_brs[pid])

            # Release unreferenced memory
            if i % 10 == 0:
                gc.collect()


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--algorithm', type=str, choices=["XODO", "PDO"],
                        required=False, default="PDO")
    parser.add_argument('--meta_iterations', type=int, required=False, default=50)
    parser.add_argument('--meta_solver', type=int, required=False, default="cfr_plus")
    parser.add_argument('--seed', type=int, required=False, default=0)
    parser.add_argument('--game_name', type=str, required=False, default="kuhn_poker",
                        choices=["leduc_poker", "kuhn_poker", "leduc_poker_dummy", "oshi_zumo", "liars_dice",
                                 "goofspiel", "python_large_kuhn_poker",
                                 "phantom_ttt", "blotto"])
    commandline_args = parser.parse_args()

    seed = commandline_args.seed
    algorithm = commandline_args.algorithm
    game_name = commandline_args.game_name
    meta_iterations = commandline_args.meta_iterations if algorithm != "XODO" else 1
    meta_solver = commandline_args.meta_solver

    # Adjust the iterations you want
    iterations = int(10000 / meta_iterations)
    data_collect_frequency = 10
    np.random.seed(seed)
    print(algorithm, game_name, meta_iterations, iterations, data_collect_frequency, seed)

    xodo = XODO(algorithm=algorithm,
                game_name=game_name,
                meta_iterations=meta_iterations,
                data_collect_frequency=data_collect_frequency,
                meta_solver=meta_solver)
    game = xodo.reset_game()
    xodo.run(game, iterations, seed)
