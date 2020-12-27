"""Model class, to be extended by specific types of models."""
import os
import sys
import yaml
from pathlib import Path
from tqdm import tqdm

import mlflow
import torch
from torch import device
from torch.utils.data import Dataset, DataLoader

sys.path.insert(0, os.path.abspath(Path(__file__).parents[2].resolve()))
from training.src.datasets.opus_dataset import OpusDataset


class Trainer:
    def __init__(self, model, config):
        self.model = model
        self.net = self.model.model
        self.device = torch.device(config["net"]["device"])
        self.model.to(self.device)
        self.config = config
        self.optimizer = self.model.optimizer(config["net"]["lr"])
        self.scheduler = self.model.scheduler()

    def iteration(self, loader, train=True):
        t_loss = 0
        perplexity = 0
        num_steps = 0
        for batch in tqdm(loader):
            if train:
                self.optimizer.zero_grad()
            loss = self.model.training_step(batch, self.device)
            if train:
                loss.backward()
                self.optimizer.step()
            t_loss += loss.item()
            perplexity += torch.exp(loss).item()
            num_steps += 1

        t_loss /= num_steps
        perplexity /= num_steps
        return t_loss, perplexity

    def run_one_epoch(self, loader, train=True):
        if train:
            self.model.train()
            t_loss, perplexity = self.iteration(loader, train)
        else:
            self.model.eval()
            with torch.no_grad():
                t_loss, perplexity = self.iteration(loader, train)

        return t_loss, perplexity

    def fit(self, train_dataset, val_dataset=None):
        experiment_id = mlflow.set_experiment(self.model.name)

        num_workers = self.config["loader"]["num_workers"]
        batch_size = self.config["net"]["batch_size"]
        epochs = self.config["net"]["epoch"]
        lr = self.config["net"]["lr"]

        train_loader = DataLoader(train_dataset, batch_size=batch_size, shuffle=True, 
                          num_workers=num_workers, pin_memory=True, drop_last=True)
        if val_dataset:
            val_loader = DataLoader(val_dataset, batch_size=batch_size, shuffle=False, 
                          num_workers=num_workers, pin_memory=True, drop_last=False)

        with mlflow.start_run(experiment_id=experiment_id):
            mlflow.log_param("batch_size", batch_size)
            mlflow.log_param("lr", lr)
            mlflow.log_param("epochs", epochs)
            mlflow.log_param("optimizer", self.optimizer)
            mlflow.log_param("scheduler", self.scheduler)
            
            for epoch in range(epochs):
                t_loss, perplexity = self.run_one_epoch(train_loader, train=True)                
                mlflow.log_metric("train loss", t_loss, step=epoch)
                mlflow.log_metric("train perplexity", perplexity, step=epoch)
                print(f"train: Epoch: {epoch + 1}, loss: {round(t_loss, 4)}, perplexity: {round(perplexity)}")
                if val_dataset:
                    val_loss, val_perplexity = self.run_one_epoch(val_loader, train=False)
                    mlflow.log_metric("val loss", val_loss, step=epoch)
                    mlflow.log_metric("val perplexity", val_perplexity, step=epoch)    
                    print(f"val: Epoch: {epoch + 1}, loss: {round(val_loss, 4)}, perplexity: {round(val_perplexity)}")
                    self.model.save_model(self.model.weights_filename)
                    if (epoch + 1) % self.config["net"]["calculate_bleu_step"] == 0:
                        bleu = self.calculate_metrics(val_dataset)
                        mlflow.log_metric("val bleu", bleu, step=epoch)    
            if self.config["net"]["use_scheduler"]:
                self.scheduler.step()
                

    def calculate_metrics(self, dataset):
        num_workers = self.config["loader"]["num_workers"]
        batch_size = self.config["net"]["batch_size"]
        loader = DataLoader(dataset, batch_size=batch_size, shuffle=False, 
                          num_workers=num_workers, pin_memory=True, drop_last=False)

        metric = 0
        samples = 0
        for batch in tqdm(loader):
            metric += self.model.calculate_metrics(batch, self.device)
            samples += len(batch["source"])
        metric_mean_val = round(metric / samples, 2)
        return metric_mean_val