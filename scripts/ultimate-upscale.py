import math
import random
import gradio as gr
from PIL import Image, ImageDraw, ImageFont, ImageOps
from modules import processing, shared, images, devices, scripts
from modules.processing import StableDiffusionProcessing
from modules.processing import Processed
from modules.shared import opts, state
from enum import Enum

class USDUJob():
    def __init__(self) -> None:
        self.mask_rect = None
        self.tile_rects = []
    def add(self, tile_rect, mask_rect) -> bool:
        if len(self.tile_rects) > 0:
            last_tile_rect = self.tile_rects[0]
            width = tile_rect[2] - tile_rect[0]
            last_width = last_tile_rect[2] - last_tile_rect[0]
            height = tile_rect[3] - tile_rect[1]
            last_height = last_tile_rect[3] - last_tile_rect[1]
            if width != last_width or height != last_height:
                return False
        if self.mask_rect != None and self.mask_rect != mask_rect:
            return False
        self.tile_rects.append(tile_rect)
        self.mask_rect = mask_rect
        return True

class USDUMode(Enum):
    LINEAR = 0
    CHESS = 1
    NONE = 2

class USDUSFMode(Enum):
    NONE = 0
    BAND_PASS = 1
    HALF_TILE = 2
    HALF_TILE_PLUS_INTERSECTIONS = 3

class USDUpscaler():

    def __init__(self, p, image, upscaler_index:int, save_redraw, save_seams_fix, tile_size) -> None:
        self.p:StableDiffusionProcessing = p
        self.image:Image = image
        self.scale_factor = max(p.width, p.height) // max(image.width, image.height)
        self.upscaler = shared.sd_upscalers[upscaler_index]
        self.redraw = USDURedraw()
        self.redraw.save = save_redraw
        self.redraw.tile_size = tile_size
        self.seams_fix = USDUSeamsFix()
        self.seams_fix.save = save_seams_fix
        self.seams_fix.tile_size = tile_size
        self.initial_info = None
        self.rows = math.ceil(self.p.height / tile_size)
        self.cols = math.ceil(self.p.width / tile_size)
        self.requested_batch_size = p.batch_size
        
    def get_factor(self, num):
        # Its just return, don't need elif
        if num == 1:
            return 2
        if num % 4 == 0:
            return 4
        if num % 3 == 0:
            return 3
        if num % 2 == 0:
            return 2
        return 0

    def get_factors(self):
        scales = []
        current_scale = 1
        current_scale_factor = self.get_factor(self.scale_factor)
        while current_scale_factor == 0:
            self.scale_factor += 1
            current_scale_factor = self.get_factor(self.scale_factor)
        while current_scale < self.scale_factor:
            current_scale_factor = self.get_factor(self.scale_factor // current_scale)
            scales.append(current_scale_factor)
            current_scale = current_scale * current_scale_factor
            if current_scale_factor == 0:
                break
        self.scales = enumerate(scales)

    def upscale(self):
        # Log info
        print(f"Canvas size: {self.p.width}x{self.p.height}")
        print(f"Image size: {self.image.width}x{self.image.height}")
        print(f"Scale factor: {self.scale_factor}")
        # Check upscaler is not empty
        if self.upscaler.name == "None":
            self.image = self.image.resize((self.p.width, self.p.height), resample=Image.LANCZOS)
            return
        # Get list with scale factors
        self.get_factors()
        # Upscaling image over all factors
        for index, value in self.scales:
            print(f"Upscaling iteration {index+1} with scale factor {value}")
            self.image = self.upscaler.scaler.upscale(self.image, value, self.upscaler.data_path)
        # Resize image to set values
        self.image = self.image.resize((self.p.width, self.p.height), resample=Image.LANCZOS)

    def setup_redraw(self, redraw_mode, padding, mask_blur):
        self.redraw.mode = USDUMode(redraw_mode)
        self.redraw.enabled = self.redraw.mode != USDUMode.NONE
        self.redraw.padding = padding
        self.p.mask_blur = mask_blur

    def setup_seams_fix(self, padding, denoise, mask_blur, width, mode):
        self.seams_fix.padding = padding
        self.seams_fix.denoise = denoise
        self.seams_fix.mask_blur = mask_blur
        self.seams_fix.width = width
        self.seams_fix.mode = USDUSFMode(mode)
        self.seams_fix.enabled = self.seams_fix.mode != USDUSFMode.NONE

    def save_image(self):
        images.save_image(self.image, self.p.outpath_samples, "", self.p.seed, self.p.prompt, opts.grid_format, info=self.initial_info, p=self.p)

    def calc_jobs_count(self):
        redraw_job_count = self.redraw.calc_jobs_count(self.image.width, self.image.height, self.rows, self.cols, self.requested_batch_size)
        seams_job_count = self.seams_fix.calc_jobs_count(self.image.width, self.image.height, self.rows, self.cols, self.requested_batch_size)
        redraw_step_count = math.ceil((self.p.denoising_strength + 0.001) * self.p.steps)
        seams_step_count = math.ceil((self.seams_fix.denoise + 0.001) * self.p.steps)
        print("expecting", redraw_step_count, "redraw steps &", seams_step_count, "seams steps")
        state.job_count = redraw_job_count + math.ceil(seams_step_count / redraw_step_count * seams_job_count)

    def print_info(self):
        print(f"Tiles amount: {self.rows * self.cols}")
        print(f"Grid: {self.rows}x{self.cols}")
        print(f"Redraw enabled: {self.redraw.enabled}")
        print(f"Seams fix mode: {self.seams_fix.mode.name}")

    def add_extra_info(self):
        self.p.extra_generation_params["Ultimate SD upscale upscaler"] = self.upscaler.name
        self.p.extra_generation_params["Ultimate SD upscale tile_size"] = self.redraw.tile_size
        self.p.extra_generation_params["Ultimate SD upscale mask_blur"] = self.p.mask_blur
        self.p.extra_generation_params["Ultimate SD upscale padding"] = self.redraw.padding

    def process(self):
        state.begin()
        self.calc_jobs_count()
        self.result_images = []
        if self.redraw.enabled:
            self.image = self.redraw.start(self.p, self.image, self.rows, self.cols)
            self.initial_info = self.redraw.initial_info
            self.result_images.append(self.image.copy())
            if self.redraw.save:
                self.save_image()

        if self.seams_fix.enabled:
            self.image = self.seams_fix.start(self.p, self.image, self.rows, self.cols)
            self.initial_info = self.seams_fix.initial_info
            self.result_images.append(self.image)
            if self.seams_fix.save:
                self.save_image()
        state.end()

class USDURedraw():

    def init_draw(self, p, width, height):
        p.inpaint_full_res = True
        p.inpaint_full_res_padding = self.padding
        p.width = math.ceil((self.tile_size+self.padding) / 64) * 64
        p.height = math.ceil((self.tile_size+self.padding) / 64) * 64
        mask = Image.new("L", (width, height), "black")
        draw = ImageDraw.Draw(mask)
        return mask, draw
    
    def init_debug_draw(self, p, width, height, tile_index):
        p.inpaint_full_res = True
        p.inpaint_full_res_padding = self.padding
        p.width = math.ceil((self.tile_size+self.padding) / 64) * 64
        p.height = math.ceil((self.tile_size+self.padding) / 64) * 64
        mask = Image.new("RGBA", (width, height), (255, 255, 255, 0))
        draw = ImageDraw.Draw(mask)
        draw.text((0, 0), str(tile_index), fill=(0, 0, 0, 255), stroke_fill=(0, 0, 0, 255), stroke_width=2)
        return mask, draw

    def linear_process(self, p, image, rows, cols):
        for yi in range(rows):
            for xi in range(cols):
                if state.interrupted:
                    break              
                tile_rect = self.calc_tile(image.width, image.height, rows, cols, xi, yi)
                cropped = image.crop(tile_rect)              
                mask, draw = self.init_draw(p, cropped.width, cropped.height)
                mask_rect = self.calc_mask_in_tile(xi, yi, cols, rows)
                draw.rectangle(mask_rect, fill="white")
                p.init_images = [cropped]
                p.image_mask = mask
                processed = processing.process_images(p)
                if (len(processed.images) > 0):
                    image.paste(processed.images[0], tile_rect)

        p.width = image.width
        p.height = image.height
        self.initial_info = processed.infotext(p, 0)

        return image
    
    def calc_mask_in_tile(self, xi, yi, cols, rows):
        start_x = 0
        end_x = 0
        if xi == 0:
            start_x = 0
            end_x = self.tile_size
        elif xi == (cols - 1):
            end_x = self.padding + self.tile_size
            start_x = self.padding
        else:
            start_x = self.padding / 2
            end_x = start_x + self.tile_size
        
        start_y = 0
        end_y = 0
        if yi == 0:
            start_y = 0
            end_y = self.tile_size
        elif yi == (rows - 1):
            start_y = self.padding
            end_y = self.padding + self.tile_size
        else:
            start_y = self.padding // 2
            end_y = start_y + self.tile_size
        rect = math.floor(start_x), math.floor(start_y), math.floor(end_x), math.floor(end_y)
        return rect

    def calc_tile(self, width, height, rows, cols, xi, yi):
        start_x = 0
        end_x = 0
        if xi == 0:
            start_x = 0
            end_x = self.tile_size + self.padding
        elif xi == (cols - 1):
            end_x = width
            start_x = end_x - self.tile_size - self.padding
        else:
            start_x = self.tile_size * xi - (self.padding // 2)
            end_x = start_x + self.padding + self.tile_size
        
        start_y = 0
        end_y = 0
        if yi == 0:
            start_y = 0
            end_y = self.tile_size + self.padding
        elif yi == (rows - 1):
            end_y = height
            start_y = end_y - self.tile_size - self.padding
        else:
            start_y = self.tile_size * yi - (self.padding // 2)
            end_y = start_y + self.padding + self.tile_size
        rect = math.floor(start_x), math.floor(start_y), math.floor(end_x), math.floor(end_y)
        return rect

    def calc_jobs_count(self, width, height, rows, cols, requested_batch_size):
        if self.enabled != True:
            return 0
        if self.mode == USDUMode.LINEAR:
            return rows * cols
        if self.mode == USDUMode.CHESS:
            self.chess_process_create_jobs(width, height, rows, cols, requested_batch_size)
            return len(self.jobs)

    def chess_process_create_jobs(self, width, height, rows, cols, requested_batch_size):
        tiles = []
        for yi in range(rows):
            for xi in range(cols):
                even_row = yi % 2 == 0
                even_column = xi % 2 == 0
                if (even_row and not even_column):
                    continue
                if (not even_row and even_column):
                    continue
                tiles.append((xi, yi))
        for yi in range(rows):
            for xi in range(cols):
                even_row = yi % 2 == 0
                even_column = xi % 2 == 0
                if (even_row and even_column):
                    continue
                if (not even_row and not even_column):
                    continue
                tiles.append((xi, yi))
        jobs = []
        while(len(tiles) > 0):
            batch_size = requested_batch_size
            actual_batch_size = 0
            job = USDUJob()
            for pair_index in range(batch_size):
                if pair_index >= len(tiles):
                    break
                pair = tiles[pair_index]
                xi = pair[0]
                yi = pair[1]
                mask_rect = self.calc_mask_in_tile(xi, yi, cols, rows)
                tile_rect = self.calc_tile(width, height, rows, cols, xi, yi)
                if False == job.add(tile_rect, mask_rect):
                    break
                actual_batch_size += 1
            tiles = tiles[actual_batch_size:]
            if (len(job.tile_rects) > 0):
                jobs.append(job)
        self.jobs = jobs
        print(len(self.jobs), "redraw chess jobs with max batch size", requested_batch_size)

    def chess_process(self, p, image, rows, cols):
        debug = False
        jobs = self.jobs
        processed_count = 0
        while(len(jobs) > 0):
            if state.interrupted:
                break
            job = jobs.pop(0)
            init_images = []
            for index in range(len(job.tile_rects)):
                init_images.append(image.crop(job.tile_rects[index]))
            p.init_images = init_images  
            tile_rect = job.tile_rects[0]
            mask, draw = self.init_draw(p, tile_rect[2] - tile_rect[0],tile_rect[3] - tile_rect[1])
            draw.rectangle(job.mask_rect, fill="white") 
            p.image_mask = mask
            p.batch_size = len(init_images)
            if debug:
                if image.mode != "RGBA":
                    image = image.convert("RGBA")
                for index in range(len(init_images)):
                    init_image = init_images[index].convert("RGBA")
                    d = ImageDraw.Draw(init_image)
                    d.text((job.mask_rect[0], job.mask_rect[1]), str(processed_count), fill=(0, 0, 0, 255), stroke_fill=(255, 255, 255, 255), stroke_width=2)
                    print("tile #", processed_count, "in rect", job.tile_rects[index], "and mask rect", job.mask_rect)
                    processed_count += 1
                    image.paste(init_image, job.tile_rects[index])
            else:        
                processed = processing.process_images(p)
                processed_count += len(processed.images)
                for index in range(len(processed.images)):
                    paste_image = processed.images[index]
                    image.paste(paste_image, job.tile_rects[index])
        p.width = image.width
        p.height = image.height
        if debug:
            self.initial_info = "debugging run"
        else:    
            self.initial_info = processed.infotext(p, 0)

        return image
    
    def start(self, p, image, rows, cols):
        self.initial_info = None
        if self.mode == USDUMode.LINEAR:
            return self.linear_process(p, image, rows, cols)
        if self.mode == USDUMode.CHESS:
            return self.chess_process(p, image, rows, cols)

class USDUSeamsFix():

    def init_draw(self, p):
        self.initial_info = None
        p.width = math.ceil((self.tile_size+self.padding) / 64) * 64
        p.height = math.ceil((self.tile_size+self.padding) / 64) * 64

    def calc_jobs_count(self, width, height, rows, cols, requested_batch_size):
        if self.enabled != True:
            return 0;
        seams_job_count = 0
        if self.mode == USDUSFMode.BAND_PASS:
            return self.rows + self.cols - 2
        self.create_jobs(width, height, rows, cols, requested_batch_size)   
        seams_job_count = len(self.row_jobs) + len(self.col_jobs) 
        if self.mode == USDUSFMode.HALF_TILE_PLUS_INTERSECTIONS:
            seams_job_count += (self.rows - 1) * (self.cols - 1)
        return seams_job_count

    def create_jobs(self, width, height, rows, cols, requested_batch_size):
        row_jobs = []
        row_tiles = []
        for yi in range(rows - 1):
            for xi in range(cols):
                even_row = yi % 2 == 0
                even_column = xi % 2 == 0
                if (even_row and not even_column):
                    continue
                if (not even_row and even_column):
                    continue
                row_tiles.append((xi, yi))
        for yi in range(rows - 1):
            for xi in range(cols):
                even_row = yi % 2 == 0
                even_column = xi % 2 == 0
                if (even_row and even_column):
                    continue
                if (not even_row and not even_column):
                    continue
                row_tiles.append((xi, yi))

        while(len(row_tiles) > 0):
            batch_size = requested_batch_size
            actual_batch_size = 0
            job = USDUJob()
            for pair_index in range(batch_size):
                if pair_index >= len(row_tiles):
                    break
                pair = row_tiles[pair_index]
                xi = pair[0]
                yi = pair[1]
                mask_rect = self.calc_mask_in_tile(xi, yi, cols, rows)
                tile_rect = self.calc_row_gradient_tile(width, height, rows, cols, xi, yi)
                if False == job.add(tile_rect, mask_rect):
                    break
                actual_batch_size += 1
            row_tiles = row_tiles[actual_batch_size:]
            if (len(job.tile_rects) > 0):
                row_jobs.append(job)
        self.row_jobs = row_jobs

        col_jobs = []
        col_tiles = []
        for yi in range(rows):
            for xi in range(cols - 1):
                even_row = yi % 2 == 0
                even_column = xi % 2 == 0
                if (even_row and not even_column):
                    continue
                if (not even_row and even_column):
                    continue
                col_tiles.append((xi, yi))
        for yi in range(rows):
            for xi in range(cols - 1):
                even_row = yi % 2 == 0
                even_column = xi % 2 == 0
                if (even_row and even_column):
                    continue
                if (not even_row and not even_column):
                    continue
                col_tiles.append((xi, yi))
                
        while(len(col_tiles) > 0):
            batch_size = requested_batch_size
            actual_batch_size = 0
            job = USDUJob()
            for pair_index in range(batch_size):
                if pair_index >= len(col_tiles):
                    break
                pair = col_tiles[pair_index]
                xi = pair[0]
                yi = pair[1]
                mask_rect = self.calc_mask_in_tile(xi, yi, cols, rows)
                tile_rect = self.calc_col_gradient_tile(width, height, rows, cols, xi, yi)
                if False == job.add(tile_rect, mask_rect):
                    break
                actual_batch_size += 1
            col_tiles = col_tiles[actual_batch_size:]
            if (len(job.tile_rects) > 0):
                col_jobs.append(job)
        self.col_jobs = col_jobs
        print(len(self.col_jobs) + len(self.row_jobs), "seams fix jobs with max batch size", requested_batch_size)

        
    def calc_mask_in_tile(self, xi, yi, cols, rows):
        start_x = 0
        end_x = 0
        if xi == 0:
            start_x = 0
            end_x = self.tile_size
        elif xi == (cols - 1):
            end_x = self.padding + self.tile_size
            start_x = self.padding
        else:
            start_x = self.padding / 2
            end_x = start_x + self.tile_size
        
        start_y = 0
        end_y = 0
        if yi == 0:
            start_y = 0
            end_y = self.tile_size
        elif yi == (rows - 1):
            start_y = self.padding
            end_y = self.padding + self.tile_size
        else:
            start_y = self.padding // 2
            end_y = start_y + self.tile_size
        rect = math.floor(start_x), math.floor(start_y), math.floor(end_x), math.floor(end_y)
        return rect

    def calc_row_gradient_tile(self, width, height, rows, cols, xi, yi):
        start_x = 0
        end_x = 0
        if xi == 0:
            start_x = 0
            end_x = self.tile_size + self.padding
        elif xi == (cols - 1):
            end_x = width
            start_x = end_x - self.tile_size - self.padding
        else:
            start_x = self.tile_size * xi - (self.padding // 2)
            end_x = start_x + self.padding + self.tile_size
        
        start_y = 0
        end_y = 0
        if yi == 0:
            start_y = 0
            end_y = self.tile_size + self.padding
        elif yi == (rows - 1):
            end_y = height
            start_y = end_y - self.tile_size - self.padding
        else:
            start_y = self.tile_size * yi - (self.padding // 2)
            end_y = start_y + self.padding + self.tile_size
        start_y += self.tile_size // 2
        end_y += self.tile_size // 2    
        rect = math.floor(start_x), math.floor(start_y), math.floor(end_x), math.floor(end_y)
        return rect

    def calc_col_gradient_tile(self, width, height, rows, cols, xi, yi):
        start_x = 0
        end_x = 0
        if xi == 0:
            start_x = 0
            end_x = self.tile_size + self.padding
        elif xi == (cols - 1):
            end_x = width
            start_x = end_x - self.tile_size - self.padding
        else:
            start_x = self.tile_size * xi - (self.padding // 2)
            end_x = start_x + self.padding + self.tile_size
        start_x += self.tile_size // 2
        end_x += self.tile_size // 2      

        start_y = 0
        end_y = 0
        if yi == 0:
            start_y = 0
            end_y = self.tile_size + self.padding
        elif yi == (rows - 1):
            end_y = height
            start_y = end_y - self.tile_size - self.padding
        else:
            start_y = self.tile_size * yi - (self.padding // 2)
            end_y = start_y + self.padding + self.tile_size
        rect = math.floor(start_x), math.floor(start_y), math.floor(end_x), math.floor(end_y)
        return rect

    def half_tile_process(self, p, image, rows, cols):
        debug = False
        self.init_draw(p)
        processed = None

        gradient = Image.linear_gradient("L")
        row_gradient = Image.new("L", (self.tile_size, self.tile_size), "black")
        row_gradient.paste(gradient.resize(
            (self.tile_size, self.tile_size//2), resample=Image.BICUBIC), (0, 0))
        row_gradient.paste(gradient.rotate(180).resize(
                (self.tile_size, self.tile_size//2), resample=Image.BICUBIC), 
                (0, self.tile_size//2))
        col_gradient = Image.new("L", (self.tile_size, self.tile_size), "black")
        col_gradient.paste(gradient.rotate(90).resize(
            (self.tile_size//2, self.tile_size), resample=Image.BICUBIC), (0, 0))
        col_gradient.paste(gradient.rotate(270).resize(
            (self.tile_size//2, self.tile_size), resample=Image.BICUBIC), (self.tile_size//2, 0))

        p.denoising_strength = self.denoise
        p.mask_blur = self.mask_blur

        processed_count = 0
        jobs = self.row_jobs
        while(len(jobs) > 0):
            if state.interrupted:
                break
            job = jobs.pop(0)
            init_images = []
            for index in range(len(job.tile_rects)):
                init_images.append(image.crop(job.tile_rects[index]))
            p.width = self.tile_size
            p.height = self.tile_size
            p.inpaint_full_res = True
            p.inpaint_full_res_padding = self.padding
            p.init_images = init_images   
            p.batch_size = len(init_images)
            tile_width = job.tile_rects[0][2] - job.tile_rects[0][0]
            tile_height = job.tile_rects[0][3] - job.tile_rects[0][1]
            mask = Image.new("RGB", (tile_width, tile_height), "black")
            mask.paste(row_gradient, (job.mask_rect[0],job.mask_rect[1]))
            if debug:
                for index in range(len(init_images)):
                    init_image = init_images[index]
                    init_image.paste(mask, (0, 0))
                    image.paste(init_image, job.tile_rects[index])
                    processed_count += 1
            else:    
                p.image_mask = mask
                processed = processing.process_images(p)
                processed_count += len(processed.images)
                for index in range(len(processed.images)):
                    image.paste(processed.images[index], job.tile_rects[index])    
        jobs = self.col_jobs
        while(len(jobs) > 0):
            if state.interrupted:
                break
            job = jobs.pop(0)
            init_images = []
            for index in range(len(job.tile_rects)):
                init_images.append(image.crop(job.tile_rects[index]))
            p.width = self.tile_size
            p.height = self.tile_size
            p.inpaint_full_res = True
            p.inpaint_full_res_padding = self.padding
            p.init_images = init_images   
            p.batch_size = len(init_images)
            tile_width = job.tile_rects[0][2] - job.tile_rects[0][0]
            tile_height = job.tile_rects[0][3] - job.tile_rects[0][1]
            mask = Image.new("RGB", (tile_width, tile_height), "black")
            mask.paste(col_gradient, (job.mask_rect[0],job.mask_rect[1]))
            if debug:
                for index in range(len(init_images)):
                    init_image = init_images[index]
                    init_image.paste(mask, (0, 0))
                    image.paste(init_image, job.tile_rects[index])
                    processed_count += 1
            else:    
                p.image_mask = mask
                processed = processing.process_images(p)
                processed_count += len(processed.images)
                for index in range(len(processed.images)):
                    image.paste(processed.images[index], job.tile_rects[index])    
    
        p.width = image.width
        p.height = image.height
        if debug:
            self.initial_info = "debugging run"
        else:    
            self.initial_info = processed.infotext(p, 0)
        return image

    def half_tile_process_corners(self, p, image, rows, cols):
        fixed_image = self.half_tile_process(p, image, rows, cols)
        processed = None
        self.init_draw(p)
        gradient = Image.radial_gradient("L").resize(
            (self.tile_size, self.tile_size), resample=Image.BICUBIC)
        gradient = ImageOps.invert(gradient)
        p.denoising_strength = self.denoise
        p.mask_blur = self.mask_blur

        for yi in range(rows-1):
            for xi in range(cols-1):
                if state.interrupted:
                    break
                p.width = self.tile_size
                p.height = self.tile_size
                p.inpaint_full_res = True
                p.inpaint_full_res_padding = 0
                mask = Image.new("L", (fixed_image.width, fixed_image.height), "black")
                mask.paste(gradient, (xi*self.tile_size + self.tile_size//2,
                                      yi*self.tile_size + self.tile_size//2))

                p.init_images = [fixed_image]
                p.image_mask = mask
                processed = processing.process_images(p)
                if (len(processed.images) > 0):
                    fixed_image = processed.images[0]

        p.width = fixed_image.width
        p.height = fixed_image.height
        if processed is not None:
            self.initial_info = processed.infotext(p, 0)

        return fixed_image

    def band_pass_process(self, p, image, cols, rows):
        
        self.init_draw(p)
        processed = None

        p.denoising_strength = self.denoise
        p.mask_blur = 0

        gradient = Image.linear_gradient("L")
        mirror_gradient = Image.new("L", (256, 256), "black")
        mirror_gradient.paste(gradient.resize((256, 128), resample=Image.BICUBIC), (0, 0))
        mirror_gradient.paste(gradient.rotate(180).resize((256, 128), resample=Image.BICUBIC), (0, 128))

        row_gradient = mirror_gradient.resize((image.width, self.width), resample=Image.BICUBIC)
        col_gradient = mirror_gradient.rotate(90).resize((self.width, image.height), resample=Image.BICUBIC)

        for xi in range(1, cols):
            if state.interrupted:
                    break
            p.width = self.width + self.padding * 2
            p.height = image.height
            p.inpaint_full_res = True
            p.inpaint_full_res_padding = self.padding
            mask = Image.new("L", (image.width, image.height), "black")
            mask.paste(col_gradient, (xi * self.tile_size -self.padding, 0))

            p.init_images = [image]
            p.image_mask = mask
            processed = processing.process_images(p)
            if (len(processed.images) > 0):
                image = processed.images[0]
        for yi in range(1, rows):
            if state.interrupted:
                    break
            p.width = image.width
            p.height = self.width + self.padding * 2
            p.inpaint_full_res = True
            p.inpaint_full_res_padding = self.padding
            mask = Image.new("L", (image.width, image.height), "black")
            mask.paste(row_gradient, (0, yi * self.tile_size - self.padding))

            p.init_images = [image]
            p.image_mask = mask
            processed = processing.process_images(p)
            if (len(processed.images) > 0):
                image = processed.images[0]

        p.width = image.width
        p.height = image.height
        if processed is not None:
            self.initial_info = processed.infotext(p, 0)

        return image

    def start(self, p, image, rows, cols):
        if USDUSFMode(self.mode) == USDUSFMode.BAND_PASS:
            return self.band_pass_process(p, image, rows, cols)
        elif USDUSFMode(self.mode) == USDUSFMode.HALF_TILE:
            return self.half_tile_process(p, image, rows, cols)
        elif USDUSFMode(self.mode) == USDUSFMode.HALF_TILE_PLUS_INTERSECTIONS:
            return self.half_tile_process_corners(p, image, rows, cols)
        else:
            return image

class Script(scripts.Script):
    def title(self):
        return "Ultimate SD upscale"

    def show(self, is_img2img):
        return is_img2img

    def ui(self, is_img2img):

        target_size_types = [
            "From img2img2 settings",
            "Custom size",
            "Scale from image size"
        ]

        seams_fix_types = [
            "None",
            "Band pass", 
            "Half tile offset pass",
            "Half tile offset pass + intersections"
        ]

        redrow_modes = [
            "Linear",
            "Chess",
            "None"
        ]
        
        info = gr.HTML(
            "<p style=\"margin-bottom:0.75em\">Will upscale the image depending on the selected target size type</p>")

        with gr.Row():
            target_size_type = gr.Dropdown(label="Target size type", choices=[k for k in target_size_types], type="index",
                                  value=next(iter(target_size_types)))

            custom_width = gr.Slider(label='Custom width', minimum=64, maximum=8192, step=64, value=2048, visible=False, interactive=True)
            custom_height = gr.Slider(label='Custom height', minimum=64, maximum=8192, step=64, value=2048, visible=False, interactive=True)
            custom_scale = gr.Slider(label='Scale', minimum=1, maximum=16, step=0.01, value=2, visible=False, interactive=True)

        gr.HTML("<p style=\"margin-bottom:0.75em\">Redraw options:</p>")
        with gr.Row():
            upscaler_index = gr.Radio(label='Upscaler', choices=[x.name for x in shared.sd_upscalers],
                                value=shared.sd_upscalers[0].name, type="index")
        with gr.Row():
            redraw_mode = gr.Dropdown(label="Type", choices=[k for k in redrow_modes], type="index", value=next(iter(redrow_modes)))
            tile_size = gr.Slider(minimum=256, maximum=2048, step=64, label='Tile size', value=512)
            mask_blur = gr.Slider(label='Mask blur', minimum=0, maximum=64, step=1, value=8)
            padding = gr.Slider(label='Padding', minimum=0, maximum=128, step=1, value=32)
        gr.HTML("<p style=\"margin-bottom:0.75em\">Seams fix:</p>")
        with gr.Row():
            seams_fix_type = gr.Dropdown(label="Type", choices=[k for k in seams_fix_types], type="index", value=next(iter(seams_fix_types)))
            seams_fix_denoise = gr.Slider(label='Denoise', minimum=0, maximum=1, step=0.01, value=0.35, visible=False, interactive=True)
            seams_fix_width = gr.Slider(label='Width', minimum=0, maximum=128, step=1, value=64, visible=False, interactive=True)
            seams_fix_mask_blur = gr.Slider(label='Mask blur', minimum=0, maximum=64, step=1, value=4, visible=False, interactive=True)
            seams_fix_padding = gr.Slider(label='Padding', minimum=0, maximum=128, step=1, value=16, visible=False, interactive=True)
        gr.HTML("<p style=\"margin-bottom:0.75em\">Save options:</p>")
        with gr.Row():
            save_upscaled_image = gr.Checkbox(label="Upscaled", value=True)
            save_seams_fix_image = gr.Checkbox(label="Seams fix", value=False)

        def select_fix_type(fix_index):
            all_visible = fix_index != 0
            mask_blur_visible = fix_index == 2 or fix_index == 3
            width_visible = fix_index == 1

            return [gr.update(visible=all_visible),
                    gr.update(visible=width_visible),
                    gr.update(visible=mask_blur_visible),
                    gr.update(visible=all_visible)]

        seams_fix_type.change(
            fn=select_fix_type,
            inputs=seams_fix_type,
            outputs=[seams_fix_denoise, seams_fix_width, seams_fix_mask_blur, seams_fix_padding]
        )

        def select_scale_type(scale_index):
            is_custom_size = scale_index == 1
            is_custom_scale = scale_index == 2

            return [gr.update(visible=is_custom_size),
                    gr.update(visible=is_custom_size),
                    gr.update(visible=is_custom_scale),
                    ]

        target_size_type.change(
            fn=select_scale_type,
            inputs=target_size_type,
            outputs=[custom_width, custom_height, custom_scale]
        )

        return [info, tile_size, mask_blur, padding, seams_fix_width, seams_fix_denoise, seams_fix_padding,
                upscaler_index, save_upscaled_image, redraw_mode, save_seams_fix_image, seams_fix_mask_blur, 
                seams_fix_type, target_size_type, custom_width, custom_height, custom_scale]

    def run(self, p, _, tile_size, mask_blur, padding, seams_fix_width, seams_fix_denoise, seams_fix_padding, 
            upscaler_index, save_upscaled_image, redraw_mode, save_seams_fix_image, seams_fix_mask_blur, 
            seams_fix_type, target_size_type, custom_width, custom_height, custom_scale):

        # Init
        processing.fix_seed(p)
        devices.torch_gc()

        p.do_not_save_grid = True
        p.do_not_save_samples = True
        p.inpaint_full_res = False

        p.inpainting_fill = 1

        seed = p.seed

        # Init image
        init_img = p.init_images[0]
        if init_img == None:
            return Processed(p, [], seed, "Empty image")
        init_img = images.flatten(init_img, opts.img2img_background_color)

        #override size
        if target_size_type == 1:
            p.width = custom_width
            p.height = custom_height
        if target_size_type == 2:
            p.width = math.ceil((init_img.width * custom_scale) / 64) * 64
            p.height = math.ceil((init_img.height * custom_scale) / 64) * 64

        # Upscaling
        upscaler = USDUpscaler(p, init_img, upscaler_index, save_upscaled_image, save_seams_fix_image, tile_size)
        upscaler.upscale()
        
        # Drawing
        upscaler.setup_redraw(redraw_mode, padding, mask_blur)
        upscaler.setup_seams_fix(seams_fix_padding, seams_fix_denoise, seams_fix_mask_blur, seams_fix_width, seams_fix_type)
        upscaler.print_info()
        upscaler.add_extra_info()
        upscaler.process()
        result_images = upscaler.result_images

        return Processed(p, result_images, seed, upscaler.initial_info if upscaler.initial_info is not None else "")
