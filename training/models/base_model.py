"""Model class, to be extended by specific types of models."""
import os
import sys
import yaml
from pathlib import Path

import mlflow
import torch
from torch import device
from torch.utils.data import Dataset, DataLoader
from transformers import AutoTokenizer
from transformers.optimization import (
    Adafactor,
    AdamW,
    get_constant_schedule,
    get_constant_schedule_with_warmup,
    get_cosine_schedule_with_warmup,
    get_cosine_with_hard_restarts_schedule_with_warmup,
    get_linear_schedule_with_warmup,
    get_polynomial_decay_schedule_with_warmup,
)

sys.path.insert(0, os.path.abspath(Path(__file__).parents[2].resolve()))
from training.models.utils import label_smoothed_nll_loss, calculate_bleu
from transformers import MarianMTModel, MarianTokenizer
from training.datasets.opus_dataset import OpusDataset


DIRNAME = Path(__file__).parents[1].resolve() / 'weights'

conf_to_scheduler = {
    "linear": get_linear_schedule_with_warmup,
    "cosine": get_cosine_schedule_with_warmup,
    "cosine_w_restarts": get_cosine_with_hard_restarts_schedule_with_warmup,
    "polynomial": get_polynomial_decay_schedule_with_warmup,
    "constant": get_constant_schedule,
    "constant_w_warmup": get_constant_schedule_with_warmup,
    }


class BaseModel:
    def __init__(self, model, tokenizer_name, config, experiments_path="./"):
        self.name = f'{self.__class__.__name__}_{tokenizer_name}'
        self.tokenizer = AutoTokenizer.from_pretrained(tokenizer_name)
        self.model = model
        self.device = torch.device(config["net"]["device"])
        self.model.to(self.device)
        self.config = config
        self.optimizer = None
        self.experiments_path = experiments_path

    @property
    def weights_filename(self) -> str:
        DIRNAME.mkdir(parents=True, exist_ok=True)
        return str(DIRNAME / f'{self.name}_weights.pth')

    def _get_lr_scheduler(self, num_training_steps):
        schedule_func = conf_to_scheduler[self.config["net"]["scheduler"]]
        if self.config["net"]["scheduler"] == "constant":
            scheduler = schedule_func(self.optimizer)
        elif self.config["net"]["scheduler"] == "constant_w_warmup":
            scheduler = schedule_func(self.optimizer, num_warmup_steps=self.config["net"]["warmup_steps"])
        else:
            scheduler = schedule_func(
                self.optimizer, num_warmup_steps=self.config["net"]["warmup_steps"], num_training_steps=num_training_steps
            )
        return scheduler

    def run_one_epoch(self, loader, train=True):
        t_loss = 0
        perplexity = 0
        num_steps = 0
        bleu = 0
        if train:
            self.model.train()
        else:
            self.model.eval()

        for batch in loader:
            if train:
                self.optimizer.zero_grad()
            src_str = batch[0]
            target_str = batch[1]
            inputs = self.tokenizer.prepare_seq2seq_batch(src_str, target_str, return_tensors="pt")  # "pt" for pytorch
            out = self.model(**inputs)
            loss = out.loss
            if train:
                loss.backward()
                self.optimizer.step()
            if not train:
                translated = self.model.generate(**self.tokenizer.prepare_seq2seq_batch(src_str, return_tensors="pt"))
                tgt_text = [self.tokenizer.decode(t, skip_special_tokens=True) for t in translated]
                bleu += calculate_bleu(tgt_text, target_str)["bleu"]
            t_loss += loss.item()
            perplexity += torch.exp(loss).item()
            num_steps += 1

        t_loss /= num_steps
        perplexity /= num_steps
        bleu /= num_steps
        return t_loss, perplexity, bleu


    def fit(self, train_dataset, val_dataset=None):
        experiment_id = mlflow.set_experiment(self.name)

        num_workers = self.config["loader"]["num_workers"]
        batch_size = self.config["net"]["batch_size"]
        epochs = self.config["net"]["epoch"]
        lr = self.config["net"]["lr"]

        if self.config["net"]["optimizer"] == "AdamW":
            self.optimizer = AdamW(self.model.parameters(), lr=lr)
        if self.config["net"]["optimizer"] == "Adafactor":
            self.optimizer = Adafactor(self.model.parameters(), lr=lr)
        else:
            self.optimizer = torch.optim.Adam(self.model.parameters(), lr=lr)

        self.scheduler = self._get_lr_scheduler(self.config["net"]["epoch"])

        if self.config["net"]["label_smoothing"] == 0:
            self.loss_fn = torch.nn.CrossEntropyLoss()
        else:
            self.loss_fn = label_smoothed_nll_loss

        train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, 
                          num_workers=num_workers, pin_memory=True, drop_last=True)
        if val_dataset:
            val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, 
                          num_workers=num_workers, pin_memory=True, drop_last=False)

        with mlflow.start_run(experiment_id=experiment_id):
            mlflow.log_param("batch_size", batch_size)
            mlflow.log_param("lr", lr)
            mlflow.log_param("epochs", epochs)
            mlflow.log_param("optimizer", self.config["net"]["optimizer"])
            mlflow.log_param("scheduler", self.config["net"]["scheduler"])
            mlflow.log_param("warmup_steps", self.config["net"]["warmup_steps"])
            mlflow.log_param("num_cycles", self.config["net"]["num_cycles"])
            
            for epoch in range(epochs):
                t_loss, perplexity, _ = self.run_one_epoch(train_loader, train=True)                
                mlflow.log_metric("train loss", t_loss, step=epoch)
                mlflow.log_metric("train perplexity", perplexity, step=epoch)
                print(f"train: Epoch: {epoch + 1}, loss: {round(t_loss, 4)}, perplexity: {round(perplexity)}")
                if val_dataset:
                    val_loss, val_perplexity, bleu = self.run_one_epoch(val_loader, train=False)
                    mlflow.log_metric("val loss", val_loss, step=epoch)
                    mlflow.log_metric("val perplexity", val_perplexity, step=epoch)    
                    mlflow.log_metric("val bleu", bleu, step=epoch)    
                    print(f"val: Epoch: {epoch + 1}, loss: {round(val_loss, 4)}, perplexity: {round(val_perplexity)}, bleu: {bleu}")
                
            self.scheduler.step()

    def evaluate():
        pass


if __name__ == "__main__":
    MODEL_NAME = 'Helsinki-NLP/opus-mt-en-ru'
    CONFIG_PATH = 'training/config.yaml'
    DATASET_PATH = 'dataset'
    TEST_FILE_EN = 'test_en'
    TEST_FILE_RU = 'test_ru'
    net = MarianMTModel.from_pretrained(MODEL_NAME)
    with open(CONFIG_PATH, "r") as ymlfile:
        cfg = yaml.load(ymlfile, Loader=yaml.FullLoader)
    cfg["net"]["epoch"] = 10
    cfg["net"]["batch_size"] = 2
    model = BaseModel(net, MODEL_NAME, cfg)

    val = OpusDataset(os.path.join(DATASET_PATH, TEST_FILE_EN), 
                      os.path.join(DATASET_PATH, TEST_FILE_RU))
    model.fit(val, val)
