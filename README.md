# TimeVQVAE
This is an official Github repository for the PyTorch implementation of TimeVQVAE from the paper ["Vector Quantized Time Series Modeling with a Bidirectional Prior Model", AISTATS 2023].

TimeVQVAE is a robust time series generation model that utilizes vector quantization for data compression into the discrete latent space (stage1) and a bidirectional transformer for the prior learning (stage2).

<p align="center">
<img src=".fig/stage1.jpg" alt="" width=100% height=100%>
</p>

<p align="center">
<img src=".fig/stage2.jpg" alt="" width=50% height=50%>
</p>

<p align="center">
<img src=".fig/iterative_decoding_process.jpg" alt="" width=100% height=100%>
</p>

<p align="center">
<img src=".fig/example_of_iterative_decoding.jpg" alt="" width=50% height=50%>
</p>


# Install

# Usage

# Citations
```
@misc{oord2018neural,
    title   = {Neural Discrete Representation Learning},
    author  = {Aaron van den Oord and Oriol Vinyals and Koray Kavukcuoglu},
    year    = {2018},
    eprint  = {1711.00937},
    archivePrefix = {arXiv},
    primaryClass = {cs.LG}
}
```
