# Copyright 2020 DeepMind Technologies Limited.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Puppeteers for puppet bots."""

import abc
from typing import Generic, Mapping, NewType, Sequence, Tuple, TypeVar

import dm_env
import immutabledict
import numpy as np

State = TypeVar('State')
PuppetGoal = NewType('PuppetGoal', np.ndarray)

_GOAL_OBSERVATION_KEY = 'GOAL'
_GOAL_DTYPE = np.int32


class Puppeteer(Generic[State], metaclass=abc.ABCMeta):
  """A puppeteer that controls the timestep forwarded to the puppet.

  Must not possess any mutable state not in `initial_state`.
  """

  @abc.abstractmethod
  def initial_state(self) -> State:
    """Returns the initial state of the puppeteer.

    Must not have any side effects.
    """

  @abc.abstractmethod
  def step(self, timestep: dm_env.TimeStep,
           prev_state: State) -> Tuple[dm_env.TimeStep, State]:
    """Steps the puppeteer.

    Must not have any side effects.

    Args:
      timestep: information from the environment.
      prev_state: the previous state of the puppeteer.

    Returns:
      timestep: the timestep to forward to the puppet.
      next_state: the state for the next step call.
    """


def puppet_timestep(timestep: dm_env.TimeStep,
                    goal: PuppetGoal) -> dm_env.TimeStep:
  """Returns a timestep with a goal observation added."""
  puppet_observation = immutabledict.immutabledict(
      timestep.observation, **{_GOAL_OBSERVATION_KEY: goal})
  return timestep._replace(observation=puppet_observation)


def puppet_goals(names: Sequence[str],
                 dtype: np.dtype = _GOAL_DTYPE) -> Mapping[str, PuppetGoal]:
  """Returns a mapping from goal name to a one-hot goal vector for a puppet.

  Args:
    names: names for each of the corresponding goals.
    dtype: dtype of the one-hot goals to return.
  """
  goals = np.eye(len(names), dtype=dtype)
  goals.setflags(write=False)
  return immutabledict.immutabledict(zip(names, goals))


_CLEAN_UP_GOALS = puppet_goals(['CLEAN', 'EAT'], dtype=np.float64)
_CLEAN_UP_CLEAN_ACTION = 8


class CleanupAlternateCleanFirst(Puppeteer[State], Generic[State]):
  """Alternates cleaning and eating goals, starting with cleaning."""

  def initial_state(self) -> State:
    """See base class."""
    return 0  # step count

  def _goal(self, step_count):
    if step_count < 250:
      return _CLEAN_UP_GOALS['CLEAN']
    elif step_count < 500:
      return _CLEAN_UP_GOALS['EAT']
    elif step_count < 750:
      return _CLEAN_UP_GOALS['CLEAN']
    else:
      return _CLEAN_UP_GOALS['EAT']

  def step(self, timestep: dm_env.TimeStep,
           prev_state: State) -> Tuple[dm_env.TimeStep, State]:
    """See base class."""
    if timestep.first():
      prev_state = self.initial_state()
    goal = self._goal(prev_state)
    next_state = prev_state + 1
    return puppet_timestep(timestep, goal), next_state


class CleanupAlternateEatFirst(Puppeteer[State], Generic[State]):
  """Alternates cleaning and eating goals, starting with eating."""

  def initial_state(self) -> State:
    """See base class."""
    return 0

  def _goal(self, step_count):
    if step_count < 250:
      return _CLEAN_UP_GOALS['EAT']
    elif step_count < 500:
      return _CLEAN_UP_GOALS['CLEAN']
    elif step_count < 750:
      return _CLEAN_UP_GOALS['EAT']
    else:
      return _CLEAN_UP_GOALS['CLEAN']

  def step(self, timestep: dm_env.TimeStep,
           prev_state: State) -> Tuple[dm_env.TimeStep, State]:
    """See base class."""
    if timestep.first():
      prev_state = self.initial_state()
    goal = self._goal(prev_state)
    next_state = prev_state + 1
    return puppet_timestep(timestep, goal), next_state


class ConditionalCleaner(Puppeteer[State], Generic[State]):
  """Cleanup puppeteer for a reciprocating agent.

  Requires the agent_slot to be in the observations.
  """

  def __init__(self, threshold: int) -> None:
    """Initializes the puppeteer.

    Args:
      threshold: number of other cleaners below which it will switch to
        cleaning.
    """
    self._threshold = threshold

  def initial_state(self) -> State:
    """See base class."""
    return dict(step_count=0, clean_until=0, cleaning=None)

  def step(self, timestep: dm_env.TimeStep,
           prev_state: State) -> Tuple[dm_env.TimeStep, State]:
    """See base class."""
    if timestep.first():
      prev_state = self.initial_state()
    observation = timestep.observation
    step_count = prev_state['step_count']
    clean_until = prev_state['clean_until']
    prev_cleaning = prev_state['cleaning']

    not_me = 1 - observation['agent_slot']
    # Must have at least 1 other agent cleaning, then I'll help for a while.
    near_river = (observation['global']['observations']['POSITION'][..., 1] < 9)

    # Smooth the cleaning binary vector across 2 timesteps.
    cleaning = observation['global']['actions'] == _CLEAN_UP_CLEAN_ACTION
    if prev_cleaning is None:
      prev_cleaning = cleaning
    smooth_cleaning = np.logical_or(cleaning, prev_cleaning)

    # AND together the cleaning, the near river, and the negated identity
    # vectors to figure out the number of other cleaners. Compare to threshold.
    if np.logical_and(not_me, np.logical_and(
        smooth_cleaning, near_river)).sum() >= self._threshold:
      clean_until = step_count + 100

    if step_count < clean_until:
      goal = _CLEAN_UP_GOALS['CLEAN']
    else:
      goal = _CLEAN_UP_GOALS['EAT']
    timestep = puppet_timestep(timestep, goal)

    next_state = dict(
        step_count=step_count + 1,
        clean_until=clean_until,
        cleaning=cleaning,
    )
    return timestep, next_state


# Note: This assumes resource 0 is "good" and resource 1 is "bad". Thus:
# For PrisonersDilemma, resource 0 is `cooperate` and resource 1 is `defect`.
# For Stag hunt, resource 0 is `stag` and resource 1 is `hare`.
# For Chicken, resource 0 is `dove` and resource 1 is `hawk`.
_TWO_RESOURCE_GOALS = puppet_goals([
    'COLLECT_COOPERATE',
    'COLLECT_DEFECT',
    'DESTROY_COOPERATE',
    'DESTROY_DEFECT',
    'INTERACT',
], dtype=np.float64)


class GrimTwoResourceInTheMatrix(Puppeteer[State], Generic[State]):
  """Puppeteer function for a GRIM strategy in two resource *_in_the_matrix."""

  def __init__(self, threshold: int) -> None:
    """Initializes the puppeteer.

    Args:
      threshold: number of defections after which it will switch behavior.
    """
    self._threshold = threshold
    self._cooperate_resource_index = 0
    self._defect_resource_index = 1

  def initial_state(self) -> State:
    """See base class."""
    partner_defections = 0
    return partner_defections

  def _get_focal_and_partner_inventory(self, timestep: dm_env.TimeStep):
    """Returns the focal and partner inventories from the latest interaction."""
    interaction_inventories = timestep.observation['INTERACTION_INVENTORIES']
    focal_inventory = interaction_inventories[0]
    partner_inventory = interaction_inventories[1]
    return focal_inventory, partner_inventory

  def _is_defection(self, inventory: np.ndarray) -> bool:
    """Returns True if `inventory` constitutes defection."""
    num_cooperate_resources = inventory[self._cooperate_resource_index]
    num_defect_resources = inventory[self._defect_resource_index]
    return num_defect_resources > num_cooperate_resources

  def step(self, timestep: dm_env.TimeStep,
           prev_state: State) -> Tuple[dm_env.TimeStep, State]:
    """See base class."""
    if timestep.first():
      prev_state = self.initial_state()
    partner_defections = prev_state

    # Accumulate partner defections over the episode.
    _, partner_inventory = self._get_focal_and_partner_inventory(timestep)
    if self._is_defection(partner_inventory):
      partner_defections += 1

    inventory = timestep.observation['INVENTORY']
    num_cooperate_resources = inventory[self._cooperate_resource_index]
    num_defect_resources = inventory[self._defect_resource_index]

    # Ready to interact if collected more of either resource than the other.
    ready_to_interact = False
    if np.abs(num_defect_resources - num_cooperate_resources) > 0:
      ready_to_interact = True

    if not ready_to_interact:
      # Collect either C or D when not ready to interact.
      if partner_defections < self._threshold:
        # When defection is below threshold, then collect cooperate resources.
        goal = _TWO_RESOURCE_GOALS['COLLECT_COOPERATE']
      else:
        # When defection exceeds threshold, then collect D resources.
        goal = _TWO_RESOURCE_GOALS['COLLECT_DEFECT']
    else:
      # Interact when ready.
      goal = _TWO_RESOURCE_GOALS['INTERACT']
    timestep = puppet_timestep(timestep, goal)
    next_state = partner_defections
    return timestep, next_state
