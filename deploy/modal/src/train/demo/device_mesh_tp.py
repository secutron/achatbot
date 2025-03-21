# https://pytorch.org/tutorials/intermediate/TP_tutorial.html

import os
import modal


app = modal.App("train-demo-mesh-device-tp")

demo_image = modal.Image.debian_slim(python_version="3.12").pip_install("torch")

MOCK_EPOCH = 5  # 模拟 5 个 epoch


@app.function(
    image=demo_image,
    gpu=os.getenv("IMAGE_GPU", "T4:2"),
    retries=0,
    # how long should we stay up with no requests?
    container_idle_timeout=15 * 60,
)
def run(model_name="linear"):
    import torch
    import torch.multiprocessing as mp

    world_size = torch.cuda.device_count()  # 使用所有可用 GPU
    if world_size < 2:
        print("需要至少 2 个 GPU 来演示张量并行")
        return

    print(f"Running model {model_name} Tensor Parallelism with {world_size} GPUs")
    torch.multiprocessing.set_start_method("spawn", force=True)
    mp.spawn(
        train_device_mesh_tensor_parallel,
        args=(
            world_size,
            model_name,
        ),
        nprocs=world_size,
        join=True,
    )


"""
modal run src/train/demo/device_mesh_tp.py
modal run src/train/demo/device_mesh_tp.py --model-name mlp
modal run src/train/demo/device_mesh_tp.py --model-name mha
"""


@app.local_entrypoint()
def main(model_name="linear"):
    run.remote(model_name)


# 训练函数
def train_device_mesh_tensor_parallel(rank, world_size, model_name="linear"):
    import os
    import torch
    import torch.nn as nn
    import torch.distributed as dist
    from torch.distributed.device_mesh import init_device_mesh
    from torch.distributed.tensor.parallel import (
        parallelize_module,
        ColwiseParallel,
        RowwiseParallel,
    )
    from torch.distributed._tensor import Shard

    # 初始化分布式环境
    def setup_distributed(rank, world_size):
        os.environ["MASTER_ADDR"] = "localhost"
        os.environ["MASTER_PORT"] = "12355"
        dist.init_process_group("nccl", rank=rank, world_size=world_size)
        torch.cuda.set_device(rank)

    # 清理分布式环境
    def cleanup():
        dist.destroy_process_group()

    # 定义单一线性层模型
    class SingleLinearModel(nn.Module):
        def __init__(self, input_size, output_size):
            super(SingleLinearModel, self).__init__()
            # self.fc = nn.Linear(input_size, output_size)
            self.weight = nn.Parameter(torch.randn(output_size, input_size))
            self.bias = nn.Parameter(torch.randn(output_size))

        def forward(self, x):  # [batch_size, input_size]
            return torch.matmul(x, self.weight.t()) + self.bias  # XW^T + b
            # return self.fc(x)

    # 定义 MLP 模型
    class MLP(nn.Module):
        def __init__(self, input_size, hidden_size, output_size):
            super(MLP, self).__init__()
            self.fc1 = nn.Linear(input_size, hidden_size)
            self.relu = nn.ReLU()
            self.fc2 = nn.Linear(hidden_size, output_size)

        def forward(self, x):
            x = self.fc1(x)
            x = self.relu(x)
            x = self.fc2(x)
            return x

    # 定义包含 MultiHeadAttention 的模型
    class AttentionModel(nn.Module):
        def __init__(self, embed_dim, num_heads, output_dim):
            super(AttentionModel, self).__init__()
            self.attn = nn.MultiheadAttention(embed_dim=embed_dim, num_heads=num_heads)
            self.fc = nn.Linear(embed_dim, output_dim)  # 输出投影层
            self.norm = nn.LayerNorm(embed_dim)

        def forward(self, x):
            attn_output, _ = self.attn(x, x, x)  # 自注意力
            x = self.norm(x + attn_output)  # 残差连接和归一化
            output = self.fc(x)  # 输出投影
            return output

    # 训练函数
    def train_tensor_parallel(rank, world_size):
        # 设置分布式环境
        setup_distributed(rank, world_size)

        # 初始化 DeviceMesh
        mesh = init_device_mesh("cuda", (world_size,))  # 1D 设备网格

        if model_name == "mlp":
            # 参数设置
            input_size = 16
            hidden_size = 32
            output_size = 8
            batch_size = 4
            device = torch.device(f"cuda:{rank}")

            # 生成模拟输入数据
            x = torch.randn(batch_size, input_size).to(device)
            target = torch.randn(batch_size, output_size).to(device)

            # 初始化模型
            model = MLP(input_size, hidden_size, output_size).to(device)

            # 定义张量并行策略
            parallel_plan = {
                "fc1": ColwiseParallel(),  # 第一层按列分割（隐藏维度）
                "fc2": RowwiseParallel(),  # 第二层按行分割（接收完整的隐藏层输出）
            }
        elif model_name == "mha":
            # 参数设置
            embed_dim = 64  # 嵌入维度
            num_heads = 8  # 注意力头数，必须能被 world_size 整除
            output_dim = 32  # 输出维度
            batch_size = 4
            seq_len = 10
            device = torch.device(f"cuda:{rank}")

            # 生成模拟输入数据
            # [seq_len, batch_size, embed_dim]
            x = torch.randn(seq_len, batch_size, embed_dim).to(device)
            target = torch.randn(seq_len, batch_size, output_dim).to(device)

            # 初始化模型
            model = AttentionModel(embed_dim, num_heads, output_dim).to(device)

            # 定义张量并行策略
            parallel_plan = {
                # MultiheadAttention 内部的 QKV 投影按列分割（头数维度）
                "attn.q_proj_weight": ColwiseParallel(),
                "attn.k_proj_weight": ColwiseParallel(),
                "attn.v_proj_weight": ColwiseParallel(),
                # 输出投影按行分割（接收完整的注意力输出）
                # "attn.out_proj": RowwiseParallel(),
                # fc 层按列分割
                # "fc": ColwiseParallel(),
            }
        else:
            # 参数设置
            input_size = 16
            output_size = 32  # 输出维度，必须足够大以分片
            batch_size = 4
            device = torch.device(f"cuda:{rank}")

            # 生成模拟输入数据
            x = torch.randn(batch_size, input_size).to(device)
            target = torch.randn(batch_size, output_size).to(device)

            # 初始化模型
            model = SingleLinearModel(input_size, output_size).to(device)

            # 定义张量并行策略
            parallel_plan = {
                "weight": RowwiseParallel(
                    input_layouts=Shard(0)
                ),  # 按行分割线性层（output_size 维度）
                "bias": RowwiseParallel(),  # 按行分割偏置(只有一个维度,output_size 维度)
            }

        # 使用 DeviceMesh 并行化模型
        model = parallelize_module(model, mesh, parallel_plan)

        # 定义损失函数和优化器
        criterion = nn.MSELoss()
        optimizer = torch.optim.SGD(model.parameters(), lr=0.01)

        # 训练步骤
        model.train()
        for epoch in range(MOCK_EPOCH):
            optimizer.zero_grad()

            # 前向传播
            output = model(x)
            loss = criterion(output, target)

            # 反向传播
            loss.backward()
            optimizer.step()

            # 同步并打印损失
            dist.barrier()
            if rank == 0:
                print(f"Epoch {epoch+1}, Loss: {loss.item():.4f}")

        cleanup()

    train_tensor_parallel(rank, world_size)
