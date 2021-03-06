import torch
import time
import logging
import torch.optim as optim
import utils.generic_policy as gp
import numpy as np
import scipy.misc

from agents.agent_observed_state import AgentObservedState
from agents.replay_memory_item import ReplayMemoryItem
from learning.auxiliary_objective.object_detection import ObjectDetection
from learning.single_client.abstract_learning import AbstractLearning
from learning.auxiliary_objective.action_prediction import ActionPrediction
from learning.auxiliary_objective.temporal_autoencoder import TemporalAutoEncoder
from utils.cuda import cuda_var
from models.incremental_model.incremental_model_recurrent_implicit_factorization_resnet import \
    IncrementalModelRecurrentImplicitFactorizationResnet


class MultiClientIncrementalContextualBanditGoalImage(AbstractLearning):
    """ Perform Contextual Bandit learning (Kakade and Langford (circa 2006) & Misra, Langford and Artzi EMNLP 2017) """

    def __init__(self, model, action_space, meta_data_util, config, constants, tensorboard):
        self.max_epoch = constants["max_epochs"]
        self.model = model
        self.action_space = action_space
        self.meta_data_util = meta_data_util
        self.config = config
        self.constants = constants
        self.num_client = config["num_client"]
        self.tensorboard = tensorboard
        self.entropy = None
        self.cross_entropy = None
        self.entropy_coef = constants["entropy_coefficient"]

        # Auxiliary Objectives
        if self.config["do_action_prediction"]:
            self.action_prediction_loss_calculator = ActionPrediction(self.model)
            self.action_prediction_loss = None
        if self.config["do_temporal_autoencoding"]:
            self.temporal_autoencoder_loss_calculator = TemporalAutoEncoder(self.model)
            self.temporal_autoencoder_loss = None
        if self.config["do_object_detection"]:
            self.object_detection_loss_calculator = ObjectDetection(self.model)
            self.object_detection_loss = None

        self.optimizer = optim.Adam(model.get_parameters(),
                                    lr=constants["learning_rate"])
        AbstractLearning.__init__(self, self.model, self.calc_loss,
                                  self.optimizer, self.config, self.constants)

    def calc_loss(self, batch_replay_items):

        agent_observation_state_ls = []
        immediate_rewards = []
        action_batch = []
        log_probabilities = []
        factor_entropy = []
        for replay_item in batch_replay_items:
            agent_observation_state_ls.append(replay_item.get_agent_observed_state())
            action_batch.append(replay_item.get_action())
            immediate_rewards.append(replay_item.get_reward())
            log_probabilities.append(replay_item.get_log_prob())
            factor_entropy.append(replay_item.get_factor_entropy())

        log_probabilities = torch.cat(log_probabilities)
        action_batch = cuda_var(torch.from_numpy(np.array(action_batch)))
        immediate_rewards = cuda_var(torch.from_numpy(np.array(immediate_rewards)).float())

        num_states = int(action_batch.size()[0])
        model_log_prob_batch = log_probabilities
        # model_log_prob_batch = self.model.get_probs_batch(agent_observation_state_ls)
        chosen_log_probs = model_log_prob_batch.gather(1, action_batch.view(-1, 1))
        reward_log_probs = immediate_rewards * chosen_log_probs.view(-1)

        gold_distribution = cuda_var(torch.FloatTensor([0.6719, 0.1457, 0.1435, 0.0387]))
        model_prob_batch = torch.exp(model_log_prob_batch)
        mini_batch_action_distribution = torch.mean(model_prob_batch, 0)

        cross_entropy = -torch.sum(gold_distribution * torch.log(mini_batch_action_distribution))
        objective = torch.sum(reward_log_probs) / num_states
        # Essentially we want the objective to increase and cross entropy to decrease
        loss = -objective + self.entropy_coef * cross_entropy
        self.cross_entropy = cross_entropy

        # Minimize the Factor Entropy if the model is implicit factorization model
        if isinstance(self.model, IncrementalModelRecurrentImplicitFactorizationResnet):
            self.mean_factor_entropy = torch.mean(torch.cat(factor_entropy))
            loss = loss + self.mean_factor_entropy
        else:
            self.mean_factor_entropy = None

        if self.config["do_action_prediction"]:
            self.action_prediction_loss = self.action_prediction_loss_calculator.calc_loss(batch_replay_items)
            if self.action_prediction_loss is not None:
                self.action_prediction_loss = self.constants["action_prediction_coeff"] * self.action_prediction_loss
                loss = loss + self.action_prediction_loss
        else:
            self.action_prediction_loss = None

        if self.config["do_temporal_autoencoding"]:
            self.temporal_autoencoder_loss = self.temporal_autoencoder_loss_calculator.calc_loss(batch_replay_items)
            if self.temporal_autoencoder_loss is not None:
                self.temporal_autoencoder_loss = \
                    self.constants["temporal_autoencoder_coeff"] * self.temporal_autoencoder_loss
                loss = loss + self.temporal_autoencoder_loss
        else:
            self.temporal_autoencoder_loss = None

        if self.config["do_object_detection"]:
            self.object_detection_loss = self.object_detection_loss_calculator.calc_loss(batch_replay_items)
            self.object_detection_loss = self.constants["object_detection_coeff"] * self.object_detection_loss
            loss = loss + self.object_detection_loss
        else:
            self.object_detection_loss = None

        return loss

    @staticmethod
    def read_goal_images(dataset, tag):
        dataset_size = len(dataset)
        images = []
        for i in range(0, dataset_size):
            img = scipy.misc.imread("goal_images/" + str(tag) + "_images/final_image_" + str(i) + ".png")
            images.append(img.swapaxes(1, 2).swapaxes(0, 1))
        return images

    def do_train(self, agent, train_dataset, tune_dataset, experiment_name):
        """ Perform training """

        clients = []
        batch_replay_items = []
        for client_ix in range(0, self.num_client):
            client = Client(agent, self.config, self.constants, self.tensorboard, client_ix, batch_replay_items)
            clients.append(client)

        dataset_iterator = DatasetIterator(train_dataset)
        epoch = 1
        action_counts = [0] * self.action_space.num_actions()

        print ("Reading images")
        start = time.time()
        train_images = self.read_goal_images(train_dataset, "train")
        tune_images = self.read_goal_images(tune_dataset, "tune")
        end = time.time()
        print ("Read all images. Time taken " + str(end - start) + " seconds. ")

        if epoch <= self.max_epoch:
            logging.info("Starting epoch %d", epoch)
            # Test on tuning data
            agent.test(tune_dataset, tune_images, tensorboard=self.tensorboard)

        probabilities_batch = [None] * self.num_client
        client_state = [None] * self.num_client

        while True:

            for client_ix in range(0, self.num_client):

                client = clients[client_ix]

                # See if the client can progress
                client_status = client.try_to_progress()
                if client_status == Client.WAITING_FOR_EXAMPLE:
                    # Provide the next example
                    data_point = dataset_iterator.get_next()
                    if data_point is None:
                        continue
                    max_num_actions = len(data_point.get_trajectory())
                    max_num_actions += self.constants["max_extra_horizon"]
                    # max_num_actions = self.constants["horizon"]
                    goal_image = train_images[dataset_iterator.datapoint_ix - 1]
                    client.accept_new_example(data_point, max_num_actions, goal_image)

                elif client_status == Client.WAITING_FOR_ACTION:

                    # Generate probabilities over actions and take action
                    log_probabilities, new_model_state, image_emb_seq = self.model.get_probs(client.get_state(),
                                                                              client.get_model_state())
                    if isinstance(self.model, IncrementalModelRecurrentImplicitFactorizationResnet):
                        factor_entropy = self.model.get_recent_factorization_entropy()
                    else:
                        factor_entropy = None
                    client.take_action(log_probabilities, new_model_state, image_emb_seq, factor_entropy)
                    # if client_state[client_ix] is None:
                    #     # This client has not waited so make it wait for 1 iteration
                    #     # Take its state and compute the probabiltiy at the end.
                    #     client_state[client_ix] = client.get_state()
                    # else:
                    #     # This client has waited so its probability must be ready.
                    #     probabilities = probabilities_batch[client_ix]
                    #     # Generate probabilities over actions and take action
                    #     # probabilities = list(torch.exp(self.model.get_probs(client.get_state()).data))
                    #     client.take_action(probabilities)
                    #     probabilities_batch[client_ix] = None
                    #     client_state[client_ix] = None

                elif client_status == Client.WAITING_TO_RECEIVE:
                    pass
                else:
                    raise AssertionError("Unknown status. Found " + str(client_status))

            # states = [state for state in client_state if state is not None]
            # if len(states) > 0:
            #     probabilities = list(torch.exp(self.model.get_probs_batch(states).data))
            #     assert len(states) == len(probabilities)
            #     ctr = 0
            #     for i in range(0, self.num_client):
            #         if client_state[i] is not None:
            #             probabilities_batch[i] = probabilities[ctr]
            #             ctr += 1
            #         else:
            #             probabilities_batch[i] = None

            # Perform update
            if len(batch_replay_items) > 32:
                loss_val = self.do_update(batch_replay_items)
                # self.action_prediction_loss_calculator.predict_action(batch_replay_items)
                del batch_replay_items[:]  # in place list clear
                # entropy_val = float(self.entropy.data[0])
                # self.tensorboard.log(entropy_val, loss_val, total_reward)
                cross_entropy = float(self.cross_entropy.data[0])
                self.tensorboard.log(cross_entropy, loss_val, 0)
                if self.action_prediction_loss is not None:
                    action_prediction_loss = float(self.action_prediction_loss.data[0])
                    self.tensorboard.log_action_prediction_loss(action_prediction_loss)
                if self.temporal_autoencoder_loss is not None:
                    temporal_autoencoder_loss = float(self.temporal_autoencoder_loss.data[0])
                    self.tensorboard.log_temporal_autoencoder_loss(temporal_autoencoder_loss)
                if self.object_detection_loss is not None:
                    object_detection_loss = float(self.object_detection_loss.data[0])
                    self.tensorboard.log_object_detection_loss(object_detection_loss)
                if self.mean_factor_entropy is not None:
                    mean_factor_entropy = float(self.mean_factor_entropy.data[0])
                    self.tensorboard.log_factor_entropy_loss(mean_factor_entropy)

            # Check if an epoch is finished. An epoch is over if all clients are waiting
            # for an example (at which point the iterator also returns none)
            epoch_completed = all([client.get_status() == Client.WAITING_FOR_EXAMPLE for client in clients])
            if epoch_completed:
                assert dataset_iterator.get_next() is None

                # Reset the iterator
                dataset_iterator.reset()

                # Save the model
                self.model.save_model(experiment_name + "/contextual_bandit_resnet_epoch_" + str(epoch))
                if epoch >= self.max_epoch:
                    break
                epoch += 1
                logging.info("Starting epoch %d", epoch)

                # Test on tuning data
                agent.test(tune_dataset, tune_images, tensorboard=self.tensorboard)


class Client:
    """ Client can be in one of the following state:
    1. Free and Waiting for new example
    2. Waiting to take the next action
    3. Waiting to receive the next image and message.

    Client operates in an automaton following the transitions below:
    Wait for a new example -> repeat [Take an action -> Wait to receive next image and message ] -> Go back to (1) """

    WAITING_FOR_EXAMPLE, WAITING_FOR_ACTION, WAITING_TO_RECEIVE = range(3)

    def __init__(self, agent, config, constants, tensorboard, client_ix, batch_replay_items):
        self.agent = agent
        self.config = config
        self.constants = constants
        self.tensorboard = tensorboard

        # Client specific information
        self.status = Client.WAITING_FOR_EXAMPLE
        self.client_ix = client_ix
        self.server = agent.servers[client_ix]
        self.metadata = None

        # Datapoint specific variable
        self.max_num_actions = None
        self.state = None
        self.goal_image = None
        self.model_state = None
        self.image_emb_seq = None
        self.current_data_point = None
        self.last_action = None
        self.last_log_prob = None
        self.factor_entropy = None
        self.num_action = 0
        self.total_reward = 0
        self.forced_stop = False
        self.batch_replay_items = batch_replay_items

    def get_state(self):
        return self.state

    def get_status(self):
        return self.status

    def get_model_state(self):
        return self.model_state

    def try_to_progress(self):

        # If in state (1) or (2) then return immediately
        if self.status == Client.WAITING_FOR_EXAMPLE or self.status == Client.WAITING_FOR_ACTION:
            return self.status

        assert self.status == Client.WAITING_TO_RECEIVE

        # If in state (3) then see if the message is available. If the message
        # is available then return to waiting for an action or a new example.
        if self.state is None:
            feedback = self.server.receive_reset_feedback_nonblocking()
        else:
            feedback = self.server.receive_feedback_nonblocking()

        if feedback is None:
            return self.status
        else:
            if self.state is None:
                # assert False, "state should not be none"
                # Feedback is in response to reset
                image, metadata = feedback

                pose = int(metadata["y_angle"] / 15.0)
                position_orientation = (metadata["x_pos"], metadata["z_pos"],
                                        metadata["y_angle"])
                self.state = AgentObservedState(instruction=self.current_data_point.instruction,
                                                config=self.config,
                                                constants=self.constants,
                                                start_image=image,
                                                previous_action=None,
                                                pose=pose,
                                                position_orientation=position_orientation,
                                                data_point=self.current_data_point,
                                                goal_image=self.goal_image)

                # Waiting for action
                self.status = Client.WAITING_FOR_ACTION
            else:
                # Feedback is in response to an action
                image, reward, metadata = feedback
                self.total_reward += reward

                # Create a replay item unless it is forced
                if not self.forced_stop:
                    replay_item = ReplayMemoryItem(
                        self.state, self.last_action, reward, log_prob=self.last_log_prob,
                        image_emb_seq=self.image_emb_seq, factor_entropy=self.factor_entropy)
                    self.batch_replay_items.append(replay_item)

                # Update the agent state
                pose = int(metadata["y_angle"] / 15.0)
                position_orientation = (metadata["x_pos"],
                                        metadata["z_pos"],
                                        metadata["y_angle"])
                self.state = self.state.update(
                    image, self.last_action, pose=pose,
                    position_orientation=position_orientation,
                    data_point=self.current_data_point)

                if self.last_action == self.agent.action_space.get_stop_action_index():
                    # Update the scores based on meta_data
                    # self.meta_data_util.log_results(metadata)

                    if self.tensorboard is not None:
                        self.tensorboard.log_all_train_errors(
                            metadata["edit_dist_error"], metadata["closest_dist_error"], metadata["stop_dist_error"])
                    self.status = Client.WAITING_FOR_EXAMPLE
                else:

                    if self.num_action >= self.max_num_actions:
                        # Send forced stop action and wait to receive
                        self._take_forced_stop()
                        self.status = Client.WAITING_TO_RECEIVE
                    else:
                        # Wait to take another action
                        self.status = Client.WAITING_FOR_ACTION

            self.metadata = metadata
            return self.status

    def accept_new_example(self, data_point, max_num_actions, goal_image):
        assert self.status == Client.WAITING_FOR_EXAMPLE
        self.state = None
        self.metadata = None
        self.model_state = None
        self.image_emb_seq = None
        self.factor_entropy = None
        self.max_num_actions = max_num_actions
        self.server.reset_nonblocking(data_point)
        self.current_data_point = data_point
        self.last_action = None
        self.last_log_prob = None
        self.num_action = 0
        self.goal_image = goal_image
        self.total_reward = 0
        self.forced_stop = False
        self.status = Client.WAITING_TO_RECEIVE

    def take_action(self, log_probabilities, new_model_state, image_emb_seq, factor_entropy):
        assert self.status == Client.WAITING_FOR_ACTION

        probability = list(torch.exp(log_probabilities.data))[0]

        self.model_state = new_model_state
        self.last_log_prob = log_probabilities
        self.image_emb_seq = image_emb_seq
        self.factor_entropy = factor_entropy

        # Use test policy to get the action
        self.last_action = gp.sample_action_from_prob(probability)
        self.num_action += 1

        # if self.metadata["goal_dist"] < 5:
        #     # Add a forced stop action to replay items
        #     imp_weight = float(probability[3])
        #     reward = 1.0
        #     print "Added with reward of " + str(reward * imp_weight)
        #     replay_item = ReplayMemoryItem(
        #         self.state, self.agent.action_space.get_stop_action_index(), reward * imp_weight,
        #         log_prob=self.last_log_prob, image_emb_seq=self.image_emb_seq, factor_entropy=self.factor_entropy)
        #     self.batch_replay_items.append(replay_item)

        if self.last_action == self.agent.action_space.get_stop_action_index():
            self.server.halt_nonblocking()
        else:
            self.server.send_action_nonblocking(self.last_action)

        self.status = Client.WAITING_TO_RECEIVE

    def _take_forced_stop(self):
        # Use test policy to get the action
        self.last_action = self.agent.action_space.get_stop_action_index()
        self.forced_stop = True
        self.server.halt_nonblocking()
        self.status = Client.WAITING_TO_RECEIVE


class DatasetIterator:

    def __init__(self, dataset, log_per_ix=100):
        self.dataset = dataset
        self.dataset_size = len(dataset)
        self.datapoint_ix = 0
        self.log_per_ix = log_per_ix

    def get_next(self):
        if self.datapoint_ix == self.dataset_size:
            return None
        else:
            datapoint = self.dataset[self.datapoint_ix]
            self.datapoint_ix += 1
            if self.log_per_ix is not None and ((self.datapoint_ix + 1) % self.log_per_ix == 0):
                logging.info("Done %d out of %d", self.datapoint_ix, self.dataset_size)
            return datapoint

    def reset(self):
        self.datapoint_ix = 0
