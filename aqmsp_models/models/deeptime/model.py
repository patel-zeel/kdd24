import os

os.environ["CUDA_VISIBLE_DEVICES"] = "3"

from os.path import join
import numpy as np
import pandas as pd
from tqdm import tqdm
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.data import Dataset, DataLoader

from astra.torch.models import MLPRegressor, SIRENRegressor

from joblib import Parallel, delayed


class DeepTime(nn.Module):
    def __init__(self, x_dim, y_dim, hidden_dims, repr_dim, dropout):
        super().__init__()
        self.mlp = SIRENRegressor(x_dim, hidden_dims, repr_dim, dropout=dropout)
        # self.mlp = MLPRegressor(x_dim, hidden_dims, repr_dim, dropout=dropout)
        self.log_noise_var = nn.Parameter(torch.tensor(np.log(0.01)))

    def forward(self, x_context, y_context, x_target):
        mean = y_context.mean(dim=1, keepdim=True)  # over datapoints dim
        std = y_context.std(dim=1, keepdim=True)  # over datapoints dim
        y_context = (y_context - mean) / std

        def single_dim_calc(x_context, y_context, x_target):
            context_repr = self.mlp(x_context)
            target_repr = self.mlp(x_target)

            # add bias term
            context_repr = torch.cat(
                [context_repr, torch.ones(context_repr.shape[0], 1, device=x_context.device)], dim=1
            )
            target_repr = torch.cat([target_repr, torch.ones(target_repr.shape[0], 1, device=x_context.device)], dim=1)

            cov = context_repr.T @ context_repr
            cov.diagonal().add_(torch.exp(self.log_noise_var))
            xty = context_repr.T @ y_context

            chol = torch.linalg.cholesky(cov)
            w = torch.cholesky_solve(xty, chol)
            y_pred = target_repr @ w
            return y_pred

        y_pred = torch.vmap(single_dim_calc, in_dims=(0, 0, 0), out_dims=0, randomness="same")(
            x_context, y_context, x_target
        )

        return y_pred * std + mean


def fit(train_data, config):
    torch.manual_seed(config.random_state)

    train_df = train_data.to_dataframe().reset_index()

    meta_dict = {}
    for feature in config.features:
        fet_min = train_data[feature].min().item()
        fet_max = train_data[feature].max().item()

        meta_dict.update(
            {
                f"{feature}_min": fet_min,
                f"{feature}_max": fet_max,
            }
        )

        train_df[feature] = (train_df[feature] - fet_min) / (fet_max - fet_min)

    class CustomDataset(Dataset):
        def __init__(self, df):
            self.df = df
            self.ts = self.df.time.unique()

        def __len__(self):
            return len(self.ts)

        def __getitem__(self, idx):
            t = self.ts[idx]
            ts_df = self.df[self.df.time == t]
            ts_df = ts_df.dropna(subset=[config.target])
            X = torch.tensor(ts_df[config.features].values, dtype=torch.float32)
            y = torch.tensor(ts_df[[config.target]].values, dtype=torch.float32)
            idx = np.random.permutation(len(X))
            num_context = int(config.context_fraction * len(X))
            X_context = X[idx[:num_context]]
            y_context = y[idx[:num_context]]
            X_target = X[idx[num_context:]]
            y_target = y[idx[num_context:]]
            return X_context, y_context, X_target, y_target

    dataset = CustomDataset(train_df)
    dataloader = DataLoader(dataset, batch_size=config.batch_size, shuffle=True)

    cnp = DeepTime(len(config.features), 1, config.hidden_dims, config.repr_dim, config.dropout).to(config.device)
    optimizer = torch.optim.Adam(cnp.parameters(), lr=config.lr)

    losses = []
    best_loss = np.inf
    for epoch in range(config.epochs):
        epoch_loss = 0
        for X_context, y_context, X_target, y_target in tqdm(dataloader):
            X_context = X_context.to(config.device)
            y_context = y_context.to(config.device)
            X_target = X_target.to(config.device)
            y_target = y_target.to(config.device)

            y_pred = cnp(X_context, y_context, X_target)
            loss = F.mse_loss(y_pred, y_target)
            epoch_loss += loss.item()

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()

        losses.append(epoch_loss / len(dataloader))

        if losses[-1] < best_loss:
            best_loss = losses[-1]
            torch.save(cnp.state_dict(), join(config.working_dir, "model.pt"))

        print(f"Epoch {epoch + 1}/{config.epochs}, Loss: {losses[-1]:.4f}")

    meta_dict["losses"] = losses
    torch.save(
        meta_dict,
        join(config.working_dir, "metadata.pt"),
    )


def predict(test_data, train_data, config):
    # load meta
    meta = torch.load(join(config.working_dir, "metadata.pt"))

    # prepare test data
    train_df = train_data.to_dataframe().reset_index()
    test_df = test_data.to_dataframe().reset_index()
    for feature in config.features:
        fet_min = meta[f"{feature}_min"]
        fet_max = meta[f"{feature}_max"]
        train_df[feature] = (train_df[feature] - fet_min) / (fet_max - fet_min)
        test_df[feature] = (test_df[feature] - fet_min) / (fet_max - fet_min)

    class CustomDataset(Dataset):
        def __init__(self, train_df, test_df):
            self.train_df = train_df
            self.test_df = test_df
            self.ts = self.train_df.time.unique()

        def __len__(self):
            return len(self.ts)

        def __getitem__(self, idx):
            t = self.ts[idx]
            train_df = self.train_df[self.train_df.time == t]
            test_df = self.test_df[self.test_df.time == t]
            train_X = torch.tensor(train_df[config.features].values, dtype=torch.float32)
            test_X = torch.tensor(test_df[config.features].values, dtype=torch.float32)
            train_y = torch.tensor(train_df[[config.target]].values, dtype=torch.float32)
            test_y = torch.tensor(test_df[[config.target]].values, dtype=torch.float32)
            return train_X, train_y, test_X, test_y

    # dataset
    dataset = CustomDataset(train_df, test_df)
    dataloader = DataLoader(dataset, batch_size=config.batch_size, shuffle=False)

    # load model
    cnp = DeepTime(len(config.features), 1, config.hidden_dims, config.repr_dim, config.dropout).to(config.device)
    cnp.load_state_dict(torch.load(join(config.working_dir, "model.pt")))
    cnp.eval()

    with torch.no_grad():
        y_pred = []
        for train_X, train_y, test_X, test_y in tqdm(dataloader):
            train_X = train_X.to(config.device)
            train_y = train_y.to(config.device)
            test_X = test_X.to(config.device)
            test_y = test_y.to(config.device)

            pred_y = cnp(train_X, train_y, test_X)
            y_pred.append(pred_y.cpu().numpy())
        y_pred = np.concatenate(y_pred, axis=0)

    test_data[f"{config.target}_pred"] = (("time", "station"), y_pred.squeeze())
    save_path = join(config.working_dir, "predictions.nc")
    test_data.to_netcdf(save_path)
    print(f"saved {config.model} predictions to {save_path}")


def fit_predict(train_data, test_data, config):
    fit(train_data, config)
    predict(test_data, train_data, config)