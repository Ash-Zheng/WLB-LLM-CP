import os
import sys
import random
from itertools import accumulate
import argparse

import torch
import torch.distributed as dist
import torch.multiprocessing as mp

parser = argparse.ArgumentParser(description="per-sequence CP test arguments")
parser.add_argument("--context_length", type=int,  default=16)   # ×1024
parser.add_argument("--batch_size",    type=int,  default=1)
parser.add_argument("--num_heads",     type=int,  default=32)
parser.add_argument("--head_dim",      type=int,  default=128)
parser.add_argument("--avg_doc_len",   type=float,default=0.25)
parser.add_argument("--std_doc_len",   type=float,default=0.50)
parser.add_argument("--cp_size",       type=int,  default=4)
parser.add_argument("--fix_seed",      type=int,  default=1)


from flash_attn.flash_attn_interface import (
    flash_attn_varlen_func, 
    _flash_attn_varlen_forward,
    _flash_attn_varlen_backward,
)

from utils import(
    compute_per_seq_metadate_combined,
    compute_per_doc_metadate_combined,
    compute_per_doc_cp_shard_doc_len,
    per_seq_correctness_evaluate,
    per_doc_correctness_evaluate,
    generate_doc_lens
)

from per_seq_cp_attn import PerSequenceCPAttention
from per_doc_cp_attn import PerDocumentCPAttention


def compute_global_fwd_bwd( 
    q, k, v, d_out, doc_lens, softmax_scale=None                
):
    # max_seqlen_q = torch.tensor([max(doc_lens)], dtype=torch.int32).to(q.device)
    # max_seqlen_k = max_seqlen_q.clone()
    max_seqlen_q = max(doc_lens)
    max_seqlen_k = max(doc_lens)
    cu_seqlens_q = torch.tensor([0] + list(accumulate(doc_lens)), dtype=torch.int32).to(q.device)
    cu_seqlens_k = cu_seqlens_q.clone()
    softmax_scale = q.shape[-1] ** -0.5 if softmax_scale is None else softmax_scale

    out, lse, _ = flash_attn_varlen_func(
        q=q,
        k=k,
        v=v,
        cu_seqlens_q=cu_seqlens_q,
        cu_seqlens_k=cu_seqlens_k,
        max_seqlen_q=max_seqlen_q,
        max_seqlen_k=max_seqlen_k,
        dropout_p=0.0,
        softmax_scale=softmax_scale,
        causal=True,
        return_attn_probs=True
    )

    dq = torch.empty_like(q)
    dk = torch.empty_like(k)
    dv = torch.empty_like(v)
    
    # compute reference global backward
    _flash_attn_varlen_backward(
        d_out,                      # dout
        q, k, v, # QKV
        out,                    # out
        lse,                    # softmax_lse
        dq, dk, dv,
        cu_seqlens_q, cu_seqlens_k,     # cu_seqlens_q / cu_seqlens_k
        max_seqlen_q, max_seqlen_k, # max_seqlen_q / max_seqlen_k
        dropout_p=0.0,                        
        softmax_scale=softmax_scale,
        causal=True,                      
        window_size=(-1, -1),               
        alibi_slopes=None,    
        deterministic=False,  
        rng_state=None,                   
        zero_tensors=False
    )

    return out, dq, dk, dv


def print_on_main(rank, content):
    if rank == 0:
        print(content)

# distributed run
def run(rank: int, world_size: int, args):
    # nccl
    os.environ["MASTER_ADDR"] = "localhost"
    os.environ["MASTER_PORT"] = "12355"
    torch.cuda.set_device(rank)
    dist.init_process_group("nccl", rank=rank, world_size=world_size)
    cp_size = world_size
    cp_group = dist.group.WORLD
    dist.barrier(device_ids=[rank])
    print_on_main(rank, "CP group initialization finished")

    # args
    batch_size      = args.batch_size              # 1
    num_heads       = args.num_heads
    head_dim        = args.head_dim
    context_length  = args.context_length * 1024   # tokens
    softmax_scale   = head_dim ** -0.5
    device = torch.device("cuda", rank)
    if args.fix_seed:
        random.seed(42)
        torch.manual_seed(42)

    # ======= Generate random input sequence consists of multiple docs =======
    if rank == 0:
        doc_lens = generate_doc_lens(args.avg_doc_len, args.std_doc_len, context_length)
        doc_lens_tensor = torch.tensor(doc_lens, dtype=torch.int32, device=torch.device(rank))
        n_doc_tensor = torch.tensor([len(doc_lens)], dtype=torch.int32, device=device)
    else:
        n_doc_tensor = torch.empty(1, dtype=torch.int32, device=device)
    dist.broadcast(n_doc_tensor, src=0, group=cp_group)

    # ======= Generate and sync random QKV tensor =======
    if rank == 0:
        q_global = torch.randn(batch_size * context_length, num_heads, head_dim, device=device, dtype=torch.bfloat16)
        k_global = torch.randn(batch_size * context_length, num_heads, head_dim, device=device, dtype=torch.bfloat16)
        v_global = torch.randn(batch_size * context_length, num_heads, head_dim, device=device, dtype=torch.bfloat16)
        d_out_global = torch.randn(batch_size * context_length, num_heads, head_dim, device=device, dtype=torch.bfloat16)
    else:
        doc_lens_tensor = torch.empty(n_doc_tensor[0].item(), dtype=torch.int32, device=device)
        q_global = torch.empty(context_length, num_heads, head_dim, dtype=torch.bfloat16, device=device)
        k_global = torch.empty(context_length, num_heads, head_dim, dtype=torch.bfloat16, device=device)
        v_global = torch.empty(context_length, num_heads, head_dim, dtype=torch.bfloat16, device=device)
        d_out_global = torch.empty(context_length, num_heads, head_dim, dtype=torch.bfloat16, device=device)

    dist.broadcast(doc_lens_tensor, src=0, group=cp_group)
    dist.broadcast(q_global, src=0, group=cp_group)
    dist.broadcast(k_global, src=0, group=cp_group)
    dist.broadcast(v_global, src=0, group=cp_group)
    dist.broadcast(d_out_global, src=0, group=cp_group)
    doc_lens = doc_lens_tensor.tolist()

    dist.barrier(device_ids=[rank])
    print_on_main(rank, "Random input generation finished")

    # ======= Compute reference output and gradients =======
    out_ref, dq_ref, dk_ref, dv_ref = compute_global_fwd_bwd(q_global, k_global, v_global, d_out_global, doc_lens, softmax_scale)
    dist.barrier(device_ids=[rank])
    print_on_main(rank, "Reference results finished")

    # ======= Per-Doc sharding fwd & bwd and correctness evaluation =======
    doc_shards = compute_per_doc_cp_shard_doc_len(doc_lens, context_length, cp_size)
    local_q_doc, local_k_doc, local_v_doc, cu_seqlens_q, cu_seqlens_k, max_seqlen_q, max_seqlen_k, kv_idx_list, local_d_out = compute_per_doc_metadate_combined(    
        context_length, 
        q_global, 
        k_global, 
        v_global, 
        doc_lens, 
        doc_shards,
        cp_size, 
        rank, 
        d_out=d_out_global
    )

    local_q_doc.retain_grad() 
    local_k_doc.retain_grad()
    local_v_doc.retain_grad()
    out = PerDocumentCPAttention.apply(
        local_q_doc, 
        local_k_doc, 
        local_v_doc,
        cu_seqlens_q, 
        cu_seqlens_k,
        max_seqlen_q, 
        max_seqlen_k,
        doc_lens,
        doc_shards,
        kv_idx_list, 
        0.0, # dropout_p
        softmax_scale, 
        "causal",
        cp_group,
        torch.cuda.current_stream(device) 
    )
    out.backward(local_d_out)
    torch.cuda.synchronize()
    per_doc_correctness_evaluate(out_ref, out, context_length, cp_size, rank, doc_lens, doc_shards)
    per_doc_correctness_evaluate(dq_ref, local_q_doc.grad, context_length, cp_size, rank, doc_lens, doc_shards, rtol=1e-1, atol=1e-1)
    per_doc_correctness_evaluate(dk_ref, local_k_doc.grad, context_length, cp_size, rank, doc_lens, doc_shards, rtol=1e-1, atol=1e-1)
    per_doc_correctness_evaluate(dv_ref, local_v_doc.grad, context_length, cp_size, rank, doc_lens, doc_shards, rtol=1e-1, atol=1e-1)
    print("Per-Doc forward & backward correntness check passed on rank:", rank)
    dist.barrier(device_ids=[rank])
    dist.destroy_process_group()


if __name__ == "__main__":
    args = parser.parse_args()

    world_size = args.cp_size
    mp.spawn(
        run,
        nprocs=world_size,
        args=(world_size, args),
        join=True,
    )