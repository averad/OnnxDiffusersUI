import argparse
import gc
import os
import re
import time
from typing import Tuple

from diffusers import OnnxStableDiffusionPipeline, OnnxStableDiffusionImg2ImgPipeline
from diffusers import DDIMScheduler, LMSDiscreteScheduler, PNDMScheduler
from diffusers import __version__ as _df_version
import gradio as gr
import numpy as np
from packaging import version
import PIL


def get_latents_from_seed(seed: int, batch_size: int, height: int, width: int) -> np.ndarray:
    latents_shape = (batch_size, 4, height // 8, width // 8)
    # Gotta use numpy instead of torch, because torch's randn() doesn't support DML
    rng = np.random.default_rng(seed)
    image_latents = rng.standard_normal(latents_shape).astype(np.float32)
    return image_latents


# gradio function
def run_diffusers(
    prompt: str,
    neg_prompt: str,
    init_image: PIL.Image.Image,
    iteration_count: int,
    batch_size: int,
    steps: int,
    guidance_scale: float,
    height: int,
    width: int,
    eta: float,
    denoise_strength: float,
    seed: str,
    image_format: str
) -> Tuple[list, str]:
    global model_name
    global current_pipe
    global pipe

    prompt.strip("\n")
    neg_prompt.strip("\n")

    # generate seeds for iterations
    if seed == "":
        rng = np.random.default_rng()
        seed = rng.integers(np.iinfo(np.uint32).max)
    else:
        try:
            seed = int(seed) & np.iinfo(np.uint32).max
        except ValueError:
            seed = hash(seed) & np.iinfo(np.uint32).max
    seeds = np.array([seed], dtype=np.uint32)  # use given seed for the first iteration
    if iteration_count > 1:
        seed_seq = np.random.SeedSequence(seed)
        seeds = np.concatenate((seeds, seed_seq.generate_state(iteration_count - 1)))

    # create and parse output directory
    output_path = "output"
    os.makedirs(output_path, exist_ok=True)
    dir_list = os.listdir(output_path)
    if len(dir_list):
        pattern = re.compile(r"([0-9][0-9][0-9][0-9][0-9][0-9])-([0-9][0-9])\..*")
        match_list = [pattern.match(f) for f in dir_list]
        next_index = max([int(m[1]) if m else -1 for m in match_list]) + 1
    else:
        next_index = 0

    sched_name = str(pipe.scheduler._class_name)
    prompts = [prompt]*batch_size
    neg_prompts = [neg_prompt]*batch_size if neg_prompt != "" else None
    images = []
    time_taken = 0
    for i in range(iteration_count):
        info = f"{next_index+i:06} | prompt: {prompt} negative prompt: {neg_prompt} | scheduler: {sched_name} " + \
            f"model: {model_name} iteration size: {iteration_count} batch size: {batch_size} steps: {steps} " + \
            f"scale: {guidance_scale} height: {height} width: {width} eta: {eta} seed: {seeds[i]}"
        if (current_pipe == "img2img"):
            info = info + f" denoise: {denoise_strength}"
        with open(os.path.join(output_path, "history.txt"), "a") as log:
            log.write(info + "\n")

        if current_pipe == "txt2img":
            # Generate our own latents so that we can provide a seed.
            latents = get_latents_from_seed(seeds[i], batch_size, height, width)

            start = time.time()
            batch_images = pipe(
                prompts, negative_prompt=neg_prompts, height=height, width=width, num_inference_steps=steps,
                guidance_scale=guidance_scale, eta=eta, latents=latents).images
            finish = time.time()
        elif current_pipe == "img2img":
            # NOTE: at this time there's no good way of setting the seed for the random noise added by the scheduler
            # np.random.seed(seeds[i])
            start = time.time()
            batch_images = pipe(
                prompts, negative_prompt=neg_prompts, init_image=init_image, height=height, width=width,
                num_inference_steps=steps, guidance_scale=guidance_scale, eta=eta, strength=denoise_strength,
                num_images_per_prompt=batch_size).images
            finish = time.time()

        short_prompt = prompt.strip("<>:\"/\\|?*\n\t")
        short_prompt = short_prompt[:99] if len(short_prompt) > 100 else short_prompt
        for j in range(batch_size):
            batch_images[j].save(os.path.join(output_path, f"{next_index+i:06}-{j:02}.{short_prompt}.{image_format}"))

        images.extend(batch_images)
        time_taken = time_taken + (finish - start)

    time_taken = time_taken / 60.0
    if iteration_count > 1:
        status = f"Run indexes {next_index:06} to {next_index+iteration_count-1:06} took {time_taken:.1f} minutes " + \
            f"to generate {iteration_count} iterations with batch size of {batch_size}. seeds: " + \
            np.array2string(seeds, separator=",")
    else:
        status = f"Run index {next_index:06} took {time_taken:.1f} minutes to generate a batch size of " + \
            f"{batch_size}. seed: {seeds[0]}"

    return images, status


def clear_click():
    global current_tab
    if current_tab == 0:
        return {
            prompt_t0: "", neg_prompt_t0: "", sch_t0: "PNDM", iter_t0: 1, batch_t0: 1, steps_t0: 16,
            guid_t0: 7.5, height_t0: 512, width_t0: 512, eta_t0: 0.0, seed_t0: "", fmt_t0: "png"}
    elif current_tab == 1:
        return {
            prompt_t1: "", neg_prompt_t1: "", image_t1: None, sch_t1: "PNDM", iter_t1: 1, batch_t1: 1, steps_t1: 16,
            guid_t1: 7.5, height_t1: 512, width_t1: 512, eta_t1: 0.0, denoise_t1: 0.8, seed_t1: "", fmt_t1: "png"}


def generate_click(
    model_drop, prompt_t0, neg_prompt_t0, sch_t0, iter_t0, batch_t0, steps_t0, guid_t0, height_t0, width_t0, eta_t0,
    seed_t0, fmt_t0, prompt_t1, neg_prompt_t1, image_t1, sch_t1, iter_t1, batch_t1, steps_t1, guid_t1, height_t1,
    width_t1, eta_t1, denoise_t1, seed_t1, fmt_t1
):
    global model_name
    global provider
    global current_tab
    global current_pipe
    global scheduler
    global pipe
    
    # reset scheduler and pipeline if model is different
    if model_name != model_drop:
        model_name = model_drop
        scheduler = None
        pipe = None
    model_path = os.path.join("model", model_name)

    # select which scheduler depending on current tab
    if current_tab == 0:
        sched_name = sch_t0
    else:
        sched_name = sch_t1

    if sched_name == "PNDM" and type(scheduler) is not PNDMScheduler:
        scheduler = PNDMScheduler.from_config(model_path, subfolder="scheduler")
    elif sched_name == "LMS" and type(scheduler) is not LMSDiscreteScheduler:
        scheduler = LMSDiscreteScheduler.from_config(model_path, subfolder="scheduler")
    elif sched_name == "DDIM" and type(scheduler) is not DDIMScheduler:
        scheduler = DDIMScheduler.from_config(model_path, subfolder="scheduler")

    # select which pipeline depending on current tab
    if current_tab == 0:
        if current_pipe != "txt2img" or pipe is None:
            pipe = OnnxStableDiffusionPipeline.from_pretrained(
                model_path, provider=provider, scheduler=scheduler)
            pipe.safety_checker = lambda images, **kwargs: (images, [False] * len(images))
            gc.collect()
        current_pipe = "txt2img"

        if type(pipe.scheduler) is not type(scheduler):
            pipe.scheduler = scheduler

        return run_diffusers(
            prompt_t0, neg_prompt_t0, None, iter_t0, batch_t0, steps_t0, guid_t0, height_t0, width_t0, eta_t0, 0,
            seed_t0, fmt_t0)
    elif current_tab == 1:
        if current_pipe != "img2img" or pipe is None:
            pipe = OnnxStableDiffusionImg2ImgPipeline.from_pretrained(
                model_path, provider=provider, scheduler=scheduler)
            pipe.safety_checker = lambda images, **kwargs: (images, [False] * len(images))
            gc.collect()
        current_pipe = "img2img"

        if type(pipe.scheduler) is not type(scheduler):
            pipe.scheduler = scheduler

        # input image resizing
        input_image = image_t1.convert("RGB")
        input_width, input_height = input_image.size
        if height_t1 / width_t1 > input_height / input_width:
            adjust_width = int(input_width * height_t1 / input_height)
            input_image = input_image.resize((adjust_width, height_t1))
            left = (adjust_width - width_t1) // 2
            right = left + width_t1
            input_image = input_image.crop((left, 0, right, height_t1))
        else:
            adjust_height = int(input_height * width_t1 / input_width)
            input_image = input_image.resize((width_t1, adjust_height))
            top = (adjust_height - height_t1) // 2
            bottom = top + height_t1
            input_image = input_image.crop((0, top, width_t1, bottom))

        return run_diffusers(
            prompt_t1, neg_prompt_t1, input_image, iter_t1, batch_t1, steps_t1, guid_t1, height_t1, width_t1, eta_t1,
            denoise_t1, seed_t1, fmt_t1)


def select_tab0():
    global current_tab
    current_tab = 0


def select_tab1():
    global current_tab
    current_tab = 1


def choose_sch(sched_name: str):
    if sched_name == "DDIM":
        return gr.update(interactive=True)
    else:
        return gr.update(interactive=False)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="gradio interface for ONNX based Stable Diffusion")
    parser.add_argument("--cpu-only", action="store_true", default=False, help="run ONNX with CPU")
    args = parser.parse_args()

    # variables for ONNX pipelines
    model_name = None
    provider = "CPUExecutionProvider" if args.cpu_only else "DmlExecutionProvider"
    current_tab = 0
    current_pipe = "txt2img"

    # diffusers objects
    scheduler = None
    pipe = None

    # check versions
    is_v_0_4 = version.parse(_df_version) >= version.parse("0.4.0")
    is_v_0_6 = version.parse(_df_version) >= version.parse("0.6.0")

    # custom css
    custom_css = """
    #gen_button {height: 100px}
    """

    # search the model folder
    model_dir = "model"
    model_list = []
    with os.scandir(model_dir) as scan_it:
        for entry in scan_it:
            if entry.is_dir():
                model_list.append(entry.name)
    default_model = model_list[0] if len(model_list) > 0 else None

    # create gradio block
    title = "Stable Diffusion ONNX"
    with gr.Blocks(title=title, css=custom_css) as demo:
        with gr.Row():
            with gr.Column(scale=1, min_width=600):
                with gr.Row():
                    model_drop = gr.Dropdown(model_list, value=default_model, label="model folder", interactive=True)
                with gr.Tab(label="txt2img") as tab0:
                    prompt_t0 = gr.Textbox(value="", lines=2, label="prompt")
                    neg_prompt_t0 = gr.Textbox(value="", lines=2, label="negative prompt", visible=is_v_0_4)
                    sch_t0 = gr.Radio(["DDIM", "LMS", "PNDM"], value="PNDM", label="Scheduler")
                    with gr.Row():
                        iter_t0 = gr.Slider(1, 24, value=1, step=1, label="iteration count")
                        batch_t0 = gr.Slider(1, 4, value=1, step=1, label="batch size")
                    steps_t0 = gr.Slider(1, 100, value=16, step=1, label="steps")
                    guid_t0 = gr.Slider(0, 50, value=7.5, step=0.1, label="guidance")
                    height_t0 = gr.Slider(384, 768, value=512, step=64, label="height")
                    width_t0 = gr.Slider(384, 768, value=512, step=64, label="width")
                    eta_t0 = gr.Slider(0, 1, value=0.0, step=0.01, label="DDIM eta", interactive=False)
                    seed_t0 = gr.Textbox(value="", max_lines=1, label="seed")
                    fmt_t0 = gr.Radio(["png", "jpg"], value="png", label="image format")
                with gr.Tab(label="img2img", visible=is_v_0_6) as tab1:
                    prompt_t1 = gr.Textbox(value="", lines=2, label="prompt")
                    neg_prompt_t1 = gr.Textbox(value="", lines=2, label="negative prompt", visible=is_v_0_4)
                    sch_t1 = gr.Radio(["DDIM", "LMS", "PNDM"], value="PNDM", label="Scheduler")
                    image_t1 = gr.Image(label="input image", type="pil")
                    with gr.Row():
                        iter_t1 = gr.Slider(1, 24, value=1, step=1, label="iteration count")
                        batch_t1 = gr.Slider(1, 4, value=1, step=1, label="batch size")
                    steps_t1 = gr.Slider(1, 100, value=16, step=1, label="steps")
                    guid_t1 = gr.Slider(0, 50, value=7.5, step=0.1, label="guidance")
                    height_t1 = gr.Slider(384, 768, value=512, step=64, label="height")
                    width_t1 = gr.Slider(384, 768, value=512, step=64, label="width")
                    eta_t1 = gr.Slider(0, 1, value=0.0, step=0.01, label="DDIM eta", interactive=False)
                    denoise_t1 = gr.Slider(0, 1, value=0.8, step=0.01, label="denoise strength")
                    seed_t1 = gr.Textbox(value="", max_lines=1, label="seed")
                    fmt_t1 = gr.Radio(["png", "jpg"], value="png", label="image format")
            with gr.Column(scale=1, min_width=600):
                with gr.Row():
                    gen_btn = gr.Button("Generate", variant="primary", elem_id="gen_button")
                    clear_btn = gr.Button("Clear", elem_id="gen_button")
                image_out = gr.Gallery(value=None, label="images")
                status_out = gr.Textbox(value="", label="status")

        # config components
        all_inputs = [
            model_drop, prompt_t0, neg_prompt_t0, sch_t0, iter_t0, batch_t0, steps_t0, guid_t0, height_t0, width_t0,
            eta_t0, seed_t0, fmt_t0, prompt_t1, neg_prompt_t1, image_t1, sch_t1, iter_t1, batch_t1, steps_t1, guid_t1,
            height_t1, width_t1, eta_t1, denoise_t1, seed_t1, fmt_t1]
        clear_btn.click(fn=clear_click, inputs=None, outputs=all_inputs, queue=False)
        gen_btn.click(fn=generate_click, inputs=all_inputs, outputs=[image_out, status_out])

        tab0.select(fn=select_tab0, inputs=None, outputs=None)
        tab1.select(fn=select_tab1, inputs=None, outputs=None)

        sch_t0.change(fn=choose_sch, inputs=sch_t0, outputs=eta_t0, queue=False)
        sch_t1.change(fn=choose_sch, inputs=sch_t1, outputs=eta_t1, queue=False)

        image_out.style(grid=2)
        # image_t1.style(height=400)

    # start gradio web interface on local host
    demo.launch()

    # use the following to launch the web interface to a private network
    # demo.queue(concurrency_count=1)
    # demo.launch(server_name="0.0.0.0")

