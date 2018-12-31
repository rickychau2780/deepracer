#
# Copyright (c) 2017 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import copy
import os
import time
from collections import OrderedDict
from distutils.dir_util import copy_tree, remove_tree
from typing import List, Tuple
import contextlib

from rl_coach.base_parameters import iterable_to_items, TaskParameters, DistributedTaskParameters, Frameworks, \
    VisualizationParameters, \
    Parameters, PresetValidationParameters
from rl_coach.core_types import TotalStepsCounter, RunPhase, PlayingStepsType, TrainingSteps, EnvironmentEpisodes, \
    EnvironmentSteps, \
    StepMethod, Transition
from rl_coach.environments.environment import Environment
from rl_coach.level_manager import LevelManager
from rl_coach.logger import screen, Logger
from rl_coach.utils import set_cpu, start_shell_command_and_wait
from rl_coach.data_stores.data_store_impl import get_data_store as data_store_creator
from rl_coach.orchestrators.kubernetes_orchestrator import RunType
from rl_coach.memories.backend.memory_impl import get_memory_backend
from rl_coach.data_stores.data_store import SyncFiles


class ScheduleParameters(Parameters):
    def __init__(self):
        super().__init__()
        self.heatup_steps = None
        self.evaluation_steps = None
        self.steps_between_evaluation_periods = None
        self.improve_steps = None


class HumanPlayScheduleParameters(ScheduleParameters):
    def __init__(self):
        super().__init__()
        self.heatup_steps = EnvironmentSteps(0)
        self.evaluation_steps = EnvironmentEpisodes(0)
        self.steps_between_evaluation_periods = EnvironmentEpisodes(100000000)
        self.improve_steps = TrainingSteps(10000000000)


class SimpleScheduleWithoutEvaluation(ScheduleParameters):
    def __init__(self, improve_steps=TrainingSteps(10000000000)):
        super().__init__()
        self.heatup_steps = EnvironmentSteps(0)
        self.evaluation_steps = EnvironmentEpisodes(0)
        self.steps_between_evaluation_periods = improve_steps
        self.improve_steps = improve_steps


class SimpleSchedule(ScheduleParameters):
    def __init__(self,
                 improve_steps=TrainingSteps(10000000000),
                 steps_between_evaluation_periods=EnvironmentEpisodes(50),
                 evaluation_steps=EnvironmentEpisodes(5)):
        super().__init__()
        self.heatup_steps = EnvironmentSteps(0)
        self.evaluation_steps = evaluation_steps
        self.steps_between_evaluation_periods = steps_between_evaluation_periods
        self.improve_steps = improve_steps


class GraphManager(object):
    """
    A graph manager is responsible for creating and initializing a graph of agents, including all its internal
    components. It is then used in order to schedule the execution of operations on the graph, such as acting and
    training.
    """
    def __init__(self,
                 name: str,
                 schedule_params: ScheduleParameters,
                 vis_params: VisualizationParameters = VisualizationParameters()):
        self.sess = None
        self.level_managers = []
        self.top_level_manager = None
        self.environments = []
        self.heatup_steps = schedule_params.heatup_steps
        self.evaluation_steps = schedule_params.evaluation_steps
        self.steps_between_evaluation_periods = schedule_params.steps_between_evaluation_periods
        self.improve_steps = schedule_params.improve_steps
        self.visualization_parameters = vis_params
        self.name = name
        self.task_parameters = None
        self._phase = self.phase = RunPhase.UNDEFINED
        self.preset_validation_params = PresetValidationParameters()
        self.reset_required = False

        # timers
        self.graph_creation_time = None
        self.last_checkpoint_saving_time = time.time()

        # counters
        self.total_steps_counters = {
            RunPhase.HEATUP: TotalStepsCounter(),
            RunPhase.TRAIN: TotalStepsCounter(),
            RunPhase.TEST: TotalStepsCounter()
        }
        self.checkpoint_id = 0

        self.checkpoint_saver = None
        self.graph_logger = Logger()
        self.data_store = None

    def create_graph(self, task_parameters: TaskParameters=TaskParameters()):
        self.graph_creation_time = time.time()
        self.task_parameters = task_parameters

        if isinstance(task_parameters, DistributedTaskParameters):
            screen.log_title("Creating graph - name: {} task id: {} type: {}".format(self.__class__.__name__,
                                                                                     task_parameters.task_index,
                                                                                     task_parameters.job_type))
        else:
            screen.log_title("Creating graph - name: {}".format(self.__class__.__name__))

        # "hide" the gpu if necessary
        if task_parameters.use_cpu:
            set_cpu()

        # create a target server for the worker and a device
        if isinstance(task_parameters, DistributedTaskParameters):
            task_parameters.worker_target, task_parameters.device = \
                self.create_worker_or_parameters_server(task_parameters=task_parameters)

        # create the graph modules
        self.level_managers, self.environments = self._create_graph(task_parameters)

        # set self as the parent of all the level managers
        self.top_level_manager = self.level_managers[0]
        for level_manager in self.level_managers:
            level_manager.parent_graph_manager = self

        # create a session (it needs to be created after all the graph ops were created)
        self.sess = None
        self.create_session(task_parameters=task_parameters)

        self._phase = self.phase = RunPhase.UNDEFINED

        self.setup_logger()

        return self

    def _create_graph(self, task_parameters: TaskParameters) -> Tuple[List[LevelManager], List[Environment]]:
        """
        Create all the graph modules and the graph scheduler
        :param task_parameters: the parameters of the task
        :return: the initialized level managers and environments
        """
        raise NotImplementedError("")

    @staticmethod
    def _create_worker_or_parameters_server_tf(task_parameters: DistributedTaskParameters):
        import tensorflow as tf
        config = tf.ConfigProto()
        config.allow_soft_placement = True  # allow placing ops on cpu if they are not fit for gpu
        config.gpu_options.allow_growth = True  # allow the gpu memory allocated for the worker to grow if needed
        config.gpu_options.per_process_gpu_memory_fraction = 0.2
        config.intra_op_parallelism_threads = 1
        config.inter_op_parallelism_threads = 1

        from rl_coach.architectures.tensorflow_components.distributed_tf_utils import \
            create_and_start_parameters_server, \
            create_cluster_spec, create_worker_server_and_device

        # create cluster spec
        cluster_spec = create_cluster_spec(parameters_server=task_parameters.parameters_server_hosts,
                                           workers=task_parameters.worker_hosts)

        # create and start parameters server (non-returning function) or create a worker and a device setter
        if task_parameters.job_type == "ps":
            create_and_start_parameters_server(cluster_spec=cluster_spec,
                                               config=config)
        elif task_parameters.job_type == "worker":
            return create_worker_server_and_device(cluster_spec=cluster_spec,
                                                   task_index=task_parameters.task_index,
                                                   use_cpu=task_parameters.use_cpu,
                                                   config=config)
        else:
            raise ValueError("The job type should be either ps or worker and not {}"
                             .format(task_parameters.job_type))

    @staticmethod
    def create_worker_or_parameters_server(task_parameters: DistributedTaskParameters):
        if task_parameters.framework_type == Frameworks.tensorflow:
            return GraphManager._create_worker_or_parameters_server_tf(task_parameters)
        elif task_parameters.framework_type == Frameworks.mxnet:
            raise NotImplementedError('Distributed training not implemented for MXNet')
        else:
            raise ValueError('Invalid framework {}'.format(task_parameters.framework_type))

    def _create_session_tf(self, task_parameters: TaskParameters):
        import tensorflow as tf
        config = tf.ConfigProto()
        config.allow_soft_placement = True  # allow placing ops on cpu if they are not fit for gpu
        config.gpu_options.allow_growth = True  # allow the gpu memory allocated for the worker to grow if needed
        # config.gpu_options.per_process_gpu_memory_fraction = 0.2
        config.intra_op_parallelism_threads = 1
        config.inter_op_parallelism_threads = 1

        if isinstance(task_parameters, DistributedTaskParameters):
            # the distributed tensorflow setting
            from rl_coach.architectures.tensorflow_components.distributed_tf_utils import create_monitored_session
            if hasattr(self.task_parameters, 'checkpoint_restore_dir') and self.task_parameters.checkpoint_restore_dir:
                checkpoint_dir = os.path.join(task_parameters.experiment_path, 'checkpoint')
                if os.path.exists(checkpoint_dir):
                    remove_tree(checkpoint_dir)
                copy_tree(task_parameters.checkpoint_restore_dir, checkpoint_dir)
            else:
                checkpoint_dir = task_parameters.checkpoint_save_dir

            self.sess = create_monitored_session(target=task_parameters.worker_target,
                                                 task_index=task_parameters.task_index,
                                                 checkpoint_dir=checkpoint_dir,
                                                 checkpoint_save_secs=task_parameters.checkpoint_save_secs,
                                                 config=config)
            # set the session for all the modules
            self.set_session(self.sess)
        else:
            self.variables_to_restore = tf.global_variables()
            # self.variables_to_restore = [v for v in self.variables_to_restore if '/online' in v.name] TODO: is this necessary?
            self.checkpoint_saver = tf.train.Saver(self.variables_to_restore)

            # regular session
            self.sess = tf.Session(config=config)

            # set the session for all the modules
            self.set_session(self.sess)

            # restore from checkpoint if given
            self.restore_checkpoint()

        # the TF graph is static, and therefore is saved once - in the beginning of the experiment
        if hasattr(self.task_parameters, 'checkpoint_save_dir') and self.task_parameters.checkpoint_save_dir:
            self.save_graph()

    def create_session(self, task_parameters: TaskParameters):
        if task_parameters.framework_type == Frameworks.tensorflow:
            self._create_session_tf(task_parameters)
        elif task_parameters.framework_type == Frameworks.mxnet:
            self.set_session(sess=None)  # Initialize all modules
            # TODO add checkpoint loading
        else:
            raise ValueError('Invalid framework {}'.format(task_parameters.framework_type))

    def save_graph(self) -> None:
        """
        Save the TF graph to a protobuf description file in the experiment directory
        :return: None
        """
        import tensorflow as tf

        # write graph
        tf.train.write_graph(tf.get_default_graph(),
                             logdir=self.task_parameters.checkpoint_save_dir,
                             name='graphdef.pb',
                             as_text=False)

    def save_onnx_graph(self) -> None:
        """
        Save the graph as an ONNX graph.
        This requires the graph and the weights checkpoint to be stored in the experiment directory.
        It then freezes the graph (merging the graph and weights checkpoint), and converts it to ONNX.
        :return: None
        """

        # collect input and output nodes
        input_nodes = []
        output_nodes = []
        for level in self.level_managers:
            for agent in level.agents.values():
                for network in agent.networks.values():
                    for input_key, input in network.online_network.inputs.items():
                        if not input_key.startswith("output_"):
                            input_nodes.append(input.name)
                    for output in network.online_network.outputs:
                        output_nodes.append(output.name)

        # TODO: make this framework agnostic
        from rl_coach.architectures.tensorflow_components.architecture import save_onnx_graph

        save_onnx_graph(input_nodes, output_nodes, self.task_parameters.checkpoint_save_dir)

    def setup_logger(self) -> None:
        # dump documentation
        logger_prefix = "{graph_name}".format(graph_name=self.name)
        self.graph_logger.set_logger_filenames(self.task_parameters.experiment_path, logger_prefix=logger_prefix,
                                               add_timestamp=True, task_id=self.task_parameters.task_index)
        if self.visualization_parameters.dump_parameters_documentation:
            self.graph_logger.dump_documentation(str(self))
        [manager.setup_logger() for manager in self.level_managers]

    @property
    def phase(self) -> RunPhase:
        """
        Get the phase of the graph
        :return: the current phase
        """
        return self._phase

    @phase.setter
    def phase(self, val: RunPhase):
        """
        Change the phase of the graph and all the hierarchy levels below it
        :param val: the new phase
        :return: None
        """
        self._phase = val
        for level_manager in self.level_managers:
            level_manager.phase = val
        for environment in self.environments:
            environment.phase = val

    @property
    def current_step_counter(self) -> TotalStepsCounter:
        return self.total_steps_counters[self.phase]

    @contextlib.contextmanager
    def phase_context(self, phase):
        old_phase = self.phase
        self.phase = phase
        yield
        self.phase = old_phase

    def set_session(self, sess) -> None:
        """
        Set the deep learning framework session for all the modules in the graph
        :return: None
        """
        [manager.set_session(sess) for manager in self.level_managers]

    def heatup(self, steps: PlayingStepsType) -> None:
        """
        Perform heatup for several steps, which means taking random actions and storing the results in memory
        :param steps: the number of steps as a tuple of steps time and steps count
        :return: None
        """
        self.verify_graph_was_created()

        if steps.num_steps > 0:
            with self.phase_context(RunPhase.HEATUP):
                screen.log_title("{}: Starting heatup".format(self.name))

                # reset all the levels before starting to heatup
                self.reset_internal_state(force_environment_reset=True)

                # act for at least steps, though don't interrupt an episode
                count_end = self.current_step_counter + steps
                while self.current_step_counter < count_end:
                    self.act(EnvironmentEpisodes(1))

    def handle_episode_ended(self) -> None:
        """
        End an episode and reset all the episodic parameters
        :return: None
        """
        self.current_step_counter[EnvironmentEpisodes] += 1

        [environment.handle_episode_ended() for environment in self.environments]

    def train(self) -> None:
        """
        Perform several training iterations for all the levels in the hierarchy
        :param steps: number of training iterations to perform
        :return: None
        """
        self.verify_graph_was_created()

        with self.phase_context(RunPhase.TRAIN):
            self.current_step_counter[TrainingSteps] += 1
            [manager.train() for manager in self.level_managers]

    def reset_internal_state(self, force_environment_reset=False) -> None:
        """
        Reset an episode for all the levels
        :param force_environment_reset: force the environment to reset the episode even if it has some conditions that
                                        tell it not to. for example, if ale life is lost, gym will tell the agent that
                                        the episode is finished but won't actually reset the episode if there are more
                                        lives available
        :return: None
        """
        self.verify_graph_was_created()

        self.reset_required = False
        [environment.reset_internal_state(force_environment_reset) for environment in self.environments]
        [manager.reset_internal_state() for manager in self.level_managers]

    def act(self, steps: PlayingStepsType, wait_for_full_episodes=False) -> None:
        """
        Do several steps of acting on the environment
        :param wait_for_full_episodes: if set, act for at least `steps`, but make sure that the last episode is complete
        :param steps: the number of steps as a tuple of steps time and steps count
        """
        self.verify_graph_was_created()

        if hasattr(self, 'data_store_params') and hasattr(self.agent_params.memory, 'memory_backend_params'):
            if self.agent_params.memory.memory_backend_params.run_type == str(RunType.ROLLOUT_WORKER):
                data_store = self.get_data_store(self.data_store_params)
                data_store.load_from_store()

        # perform several steps of playing
        count_end = self.current_step_counter + steps
        result = None
        while self.current_step_counter < count_end or (wait_for_full_episodes and result is not None and not result.game_over):
            # reset the environment if the previous episode was terminated
            if self.reset_required:
                self.reset_internal_state()

            steps_begin = self.environments[0].total_steps_counter
            result = self.top_level_manager.step(None)
            steps_end = self.environments[0].total_steps_counter

            # add the diff between the total steps before and after stepping, such that environment initialization steps
            # (like in Atari) will not be counted.
            # We add at least one step so that even if no steps were made (in case no actions are taken in the training
            # phase), the loop will end eventually.
            self.current_step_counter[EnvironmentSteps] += max(1, steps_end - steps_begin)

            if result.game_over:
                self.handle_episode_ended()
                self.reset_required = True

    def train_and_act(self, steps: StepMethod) -> None:
        """
        Train the agent by doing several acting steps followed by several training steps continually
        :param steps: the number of steps as a tuple of steps time and steps count
        :return: None
        """
        self.verify_graph_was_created()

        # perform several steps of training interleaved with acting
        if steps.num_steps > 0:
            with self.phase_context(RunPhase.TRAIN):
                self.reset_internal_state(force_environment_reset=True)

                count_end = self.current_step_counter + steps
                while self.current_step_counter < count_end:
                    # The actual steps being done on the environment are decided by the agents themselves.
                    # This is just an high-level controller.
                    self.act(EnvironmentSteps(1))
                    self.train()
                    self.occasionally_save_checkpoint()

    def sync(self) -> None:
        """
        Sync the global network parameters to the graph
        :return:
        """
        [manager.sync() for manager in self.level_managers]

    def evaluate(self, steps: PlayingStepsType, keep_networks_in_sync: bool=False) -> bool:
        """
        Perform evaluation for several steps
        :param steps: the number of steps as a tuple of steps time and steps count
        :param keep_networks_in_sync: sync the network parameters with the global network before each episode
        :return: bool, True if the target reward and target success has been reached
        """
        self.verify_graph_was_created()

        if steps.num_steps > 0:
            with self.phase_context(RunPhase.TEST):
                # reset all the levels before starting to evaluate
                self.reset_internal_state(force_environment_reset=True)
                self.sync()

                # act for at least `steps`, though don't interrupt an episode
                count_end = self.current_step_counter + steps
                while self.current_step_counter < count_end:
                    self.act(EnvironmentEpisodes(1))
                    self.sync()
        if self.should_stop():
            if self.task_parameters.checkpoint_save_dir:
                open(os.path.join(self.task_parameters.checkpoint_save_dir, SyncFiles.FINISHED.value), 'w').close()
            if hasattr(self, 'data_store_params'):
                data_store = self.get_data_store(self.data_store_params)
                data_store.save_to_store()

            screen.success("Reached required success rate. Exiting.")
            return True
        return False

    def improve(self):
        """
        The main loop of the run.
        Defined in the following steps:
        1. Heatup
        2. Repeat:
            2.1. Repeat:
                2.1.1. Act
                2.1.2. Train
                2.1.3. Possibly save checkpoint
            2.2. Evaluate
        :return: None
        """

        self.verify_graph_was_created()

        # initialize the network parameters from the global network
        self.sync()

        # heatup
        self.heatup(self.heatup_steps)

        # improve
        if self.task_parameters.task_index is not None:
            screen.log_title("Starting to improve {} task index {}".format(self.name, self.task_parameters.task_index))
        else:
            screen.log_title("Starting to improve {}".format(self.name))

        count_end = self.total_steps_counters[RunPhase.TRAIN] + self.improve_steps
        while self.total_steps_counters[RunPhase.TRAIN] < count_end:
            self.train_and_act(self.steps_between_evaluation_periods)
            if self.evaluate(self.evaluation_steps):
                break

    def _restore_checkpoint_tf(self, checkpoint_dir: str):
        import tensorflow as tf
        checkpoint = tf.train.get_checkpoint_state(checkpoint_dir)
        screen.log_title("Loading checkpoint: {}".format(checkpoint.model_checkpoint_path))
        variables = {}
        for var_name, _ in tf.contrib.framework.list_variables(checkpoint_dir):
            # Load the variable
            var = tf.contrib.framework.load_variable(checkpoint_dir, var_name)

            # Set the new name
            new_name = var_name
            new_name = new_name.replace('global/', 'online/')
            variables[new_name] = var

        for v in self.variables_to_restore:
            self.sess.run(v.assign(variables[v.name.split(':')[0]]))

    def restore_checkpoint(self):
        self.verify_graph_was_created()

        # TODO: find better way to load checkpoints that were saved with a global network into the online network
        if hasattr(self.task_parameters, 'checkpoint_restore_dir') and self.task_parameters.checkpoint_restore_dir:
            if self.task_parameters.framework_type == Frameworks.tensorflow:
                self._restore_checkpoint_tf(self.task_parameters.checkpoint_restore_dir)
            elif self.task_parameters.framework_type == Frameworks.mxnet:
                # TODO implement checkpoint restore
                pass
            else:
                raise ValueError('Invalid framework {}'.format(self.task_parameters.framework_type))

    def occasionally_save_checkpoint(self):
        # only the chief process saves checkpoints
        if self.task_parameters.checkpoint_save_secs \
                and time.time() - self.last_checkpoint_saving_time >= self.task_parameters.checkpoint_save_secs \
                and (self.task_parameters.task_index == 0  # distributed
                     or self.task_parameters.task_index is None  # single-worker
                     ):
            self.save_checkpoint()

    def save_checkpoint(self):
        if self.task_parameters.checkpoint_save_dir is None:
            self.task_parameters.checkpoint_save_dir = os.path.join(self.task_parameters.experiment_path, 'checkpoint')
        checkpoint_path = os.path.join(self.task_parameters.checkpoint_save_dir,
                                       "{}_Step-{}.ckpt".format(
                                           self.checkpoint_id,
                                           self.total_steps_counters[RunPhase.TRAIN][EnvironmentSteps]))
        if not isinstance(self.task_parameters, DistributedTaskParameters):
            if self.checkpoint_saver is not None:
                saved_checkpoint_path = self.checkpoint_saver.save(self.sess, checkpoint_path)
            else:
                saved_checkpoint_path = "<Not Saved>"
        else:
            saved_checkpoint_path = checkpoint_path

        # this is required in order for agents to save additional information like a DND for example
        [manager.save_checkpoint(self.checkpoint_id) for manager in self.level_managers]

        # the ONNX graph will be stored only if checkpoints are stored and the -onnx flag is used
        if self.task_parameters.export_onnx_graph:
            self.save_onnx_graph()

        screen.log_dict(
            OrderedDict([
                ("Saving in path", saved_checkpoint_path),
            ]),
            prefix="Checkpoint"
        )

        self.checkpoint_id += 1
        self.last_checkpoint_saving_time = time.time()

        if hasattr(self, 'data_store_params'):
            data_store = self.get_data_store(self.data_store_params)
            data_store.save_to_store()

    def verify_graph_was_created(self):
        """
        Verifies that the graph was already created, and if not, it creates it with the default task parameters
        :return: None
        """
        if self.graph_creation_time is None:
            self.create_graph()

    def __str__(self):
        result = ""
        for key, val in self.__dict__.items():
            params = ""
            if isinstance(val, list) or isinstance(val, dict) or isinstance(val, OrderedDict):
                items = iterable_to_items(val)
                for k, v in items:
                    params += "{}: {}\n".format(k, v)
            else:
                params = val
            result += "{}: \n{}\n".format(key, params)

        return result

    def should_train(self) -> bool:
        return any([manager.should_train() for manager in self.level_managers])

    # TODO-remove - this is a temporary flow, used by the trainer worker, duplicated from observe() - need to create
    #               an external trainer flow reusing the existing flow and methods [e.g. observe(), step(), act()]
    def emulate_act_on_trainer(self, steps: PlayingStepsType, transition: Transition) -> None:
        """
        This emulates the act using the transition obtained from the rollout worker on the training worker
        in case of distributed training.
        Do several steps of acting on the environment
        :param steps: the number of steps as a tuple of steps time and steps count
        """
        self.verify_graph_was_created()

        # perform several steps of playing
        count_end = self.current_step_counter + steps
        while self.current_step_counter < count_end:
            # reset the environment if the previous episode was terminated
            if self.reset_required:
                self.reset_internal_state()

            steps_begin = self.environments[0].total_steps_counter
            self.top_level_manager.emulate_step_on_trainer(transition)
            steps_end = self.environments[0].total_steps_counter

            # add the diff between the total steps before and after stepping, such that environment initialization steps
            # (like in Atari) will not be counted.
            # We add at least one step so that even if no steps were made (in case no actions are taken in the training
            # phase), the loop will end eventually.
            self.current_step_counter[EnvironmentSteps] += max(1, steps_end - steps_begin)

            if transition.game_over:
                self.handle_episode_ended()
                self.reset_required = True

    def fetch_from_worker(self, num_consecutive_playing_steps=None):
        if hasattr(self, 'memory_backend'):
            for transition in self.memory_backend.fetch(num_consecutive_playing_steps):
                self.emulate_act_on_trainer(EnvironmentSteps(1), transition)

    def setup_memory_backend(self) -> None:
        if hasattr(self.agent_params.memory, 'memory_backend_params'):
            self.memory_backend = get_memory_backend(self.agent_params.memory.memory_backend_params)

    def should_stop(self) -> bool:
        return all([manager.should_stop() for manager in self.level_managers])

    def get_data_store(self, param):
        if self.data_store:
            return self.data_store

        return data_store_creator(param)

