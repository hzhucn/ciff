import os
import numpy as np
import torch

from agents.agent_observed_state import AgentObservedState
from models.incremental_model.abstract_incremental_model import AbstractIncrementalModel
from models.incremental_module.incremental_recurrence_chaplot_module import IncrementalRecurrenceChaplotModule
from models.incremental_module.tmp_house_incremental_misra_final import TmpHouseIncrementalMisraFinal
from models.incremental_module.tmp_house_incremental_multimodal_chaplot_module import \
    TmpHouseIncrementalMultimodalChaplotModule
from models.module.chaplot_image_module import ChaplotImageModule
from models.module.chaplot_house_text_module import ChaplotHouseTextModule
from models.module.goal_prediction_module import GoalPredictionModule
from models.module.misra_image_module import MisraImageModule
from models.module.object_detection_module import ObjectDetectionModule
from models.module.symbolic_language_prediction_module import SymbolicLanguagePredictionModule
from models.module.action_prediction_module import ActionPredictionModule
from models.module.temporal_autoencoder_module import TemporalAutoencoderModule
from utils.cuda import cuda_var
from utils.nav_drone_landmarks import get_all_landmark_names


class TmpHouseMisraBaseline(AbstractIncrementalModel):
    def __init__(self, config, constants):
        AbstractIncrementalModel.__init__(self, config, constants)
        self.none_action = config["num_actions"]
        self.image_module = MisraImageModule(
            image_emb_size=constants["image_emb_dim"],
            input_num_channels=3*5,
            image_height=config["image_height"],
            image_width=config["image_width"],
            using_recurrence=True)
        self.num_cameras = 1
        if config["use_pointer_model"]:
            raise NotImplementedError()
        else:
            self.text_module = ChaplotHouseTextModule(
                emb_dim=32,
                hidden_dim=256,
                vocab_size=config["vocab_size"],
                image_height=config["image_height"],
                image_width=config["image_width"])

        self.final_module = TmpHouseIncrementalMisraFinal(
            image_module=self.image_module,
            text_module=self.text_module)
        if torch.cuda.is_available():
            self.image_module.cuda()
            self.text_module.cuda()
            self.final_module.cuda()

    def get_probs_batch(self, agent_observed_state_list, mode=None):
        raise NotImplementedError()

    def get_probs(self, agent_observed_state, model_state, mode=None, volatile=False):

        assert isinstance(agent_observed_state, AgentObservedState)
        agent_observed_state_list = [agent_observed_state]

        # Extract the last 4 images or add dummy paddings
        image_seqs = [[aos.get_image()]
                      for aos in agent_observed_state_list]
        image_batch = cuda_var(torch.from_numpy(np.array(image_seqs)).float(), volatile)

        instructions = [aos.get_instruction()
                        for aos in agent_observed_state_list]
        instructions_batch = cuda_var(torch.from_numpy(np.array(instructions)).long())

        time = agent_observed_state.time_step
        time = cuda_var(torch.from_numpy(np.array([time])).long())

        probs_batch, new_model_state, image_emb_seq, state_feature = self.final_module(
            image_batch, instructions_batch, time, mode, model_state)
        return probs_batch, new_model_state, image_emb_seq, state_feature

    def init_weights(self):
        self.text_module.init_weights()
        self.image_module.init_weights()
        self.final_module.init_weights()

    def share_memory(self):
        self.image_module.share_memory()
        self.text_module.share_memory()
        self.final_module.share_memory()

    def get_state_dict(self):
        nested_state_dict = dict()
        nested_state_dict["image_module"] = self.image_module.state_dict()
        nested_state_dict["text_module"] = self.text_module.state_dict()
        nested_state_dict["final_module"] = self.final_module.state_dict()
        return nested_state_dict

    def load_from_state_dict(self, nested_state_dict):
        self.image_module.load_state_dict(nested_state_dict["image_module"])
        self.text_module.load_state_dict(nested_state_dict["text_module"])
        self.final_module.load_state_dict(nested_state_dict["final_module"])

    def load_resnet_model(self, load_dir):
        if torch.cuda.is_available():
            torch_load = torch.load
        else:
            torch_load = lambda f_: torch.load(f_, map_location=lambda s_, l_: s_)
        image_module_path = os.path.join(load_dir, "image_module_state.bin")
        self.image_module.load_state_dict(torch_load(image_module_path))

    def fix_resnet(self):
        self.image_module.fix_resnet()

    def load_lstm_model(self, load_dir):
        if torch.cuda.is_available():
            torch_load = torch.load
        else:
            torch_load = lambda f_: torch.load(f_, map_location=lambda s_, l_: s_)
        text_module_path = os.path.join(load_dir, "text_module_state.bin")
        self.text_module.load_state_dict(torch_load(text_module_path))

    def load_saved_model(self, load_dir):
        if torch.cuda.is_available():
            torch_load = torch.load
        else:
            torch_load = lambda f_: torch.load(f_, map_location=lambda s_, l_: s_)
        image_module_path = os.path.join(load_dir, "image_module_state.bin")
        self.image_module.load_state_dict(torch_load(image_module_path))
        text_module_path = os.path.join(load_dir, "text_module_state.bin")
        self.text_module.load_state_dict(torch_load(text_module_path))
        # action_module_path = os.path.join(load_dir, "action_module_state.bin")
        # self.action_module.load_state_dict(torch_load(action_module_path))
        final_module_path = os.path.join(load_dir, "final_module_state.bin")
        self.final_module.load_state_dict(torch_load(final_module_path))

    def save_model(self, save_dir):
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        # save state file for image nn
        image_module_path = os.path.join(save_dir, "image_module_state.bin")
        torch.save(self.image_module.state_dict(), image_module_path)
        # save state file for image recurrence nn
        # save state file for text nn
        text_module_path = os.path.join(save_dir, "text_module_state.bin")
        torch.save(self.text_module.state_dict(), text_module_path)
        # save state file for action emb
        # action_module_path = os.path.join(save_dir, "action_module_state.bin")
        # torch.save(self.action_module.state_dict(), action_module_path)
        # save state file for final nn
        final_module_path = os.path.join(save_dir, "final_module_state.bin")
        torch.save(self.final_module.state_dict(), final_module_path)
        # save the auxiliary models

    def get_parameters(self):
        # parameters = list(self.image_module.parameters())
        # parameters += list(self.image_recurrence_module.parameters())
        # parameters += list(self.text_module.parameters())
        parameters = list(self.final_module.parameters())

        return parameters

    def get_named_parameters(self):
        # named_parameters = list(self.image_module.named_parameters())
        # named_parameters += list(self.image_recurrence_module.named_parameters())
        # named_parameters += list(self.text_module.named_parameters())
        named_parameters = list(self.final_module.named_parameters())
        return named_parameters
