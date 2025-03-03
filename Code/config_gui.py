from multiprocessing import Queue, Process
import threading
import cvzone
import cv2
import serial
import time
import tkinter as tk
from PIL import Image, ImageTk
import numpy as np
from numpy import pi, cos, sin, arcsin, sqrt
import configparser


# ------------------------------------------- Ball tracking and position control -------------------------------------------
"""
Run camera capture and GUI in parallel 
"""

# Camera
# IDs should be from 0 and up. Internal webcam is probably 0, USB webcam will then be 1
cap_id = 1
cam_exposure = -8 # Set as low as possible to get 30fps
IMG_W = 1280
IMG_H = 720

x, y = 0, 0 # Mouse click event coords
plat_c, plat_r = [0,0], 0 # Platform definition
col_mask = {'hmin': 0, 'smin': 0, 'vmin': 0, 'hmax': 255, 'smax': 255, 'vmax': 255}

plat_actual_r = 0.175 # Actual radius in m

# Config file handling
config_file = configparser.ConfigParser()
config_file.read("config.ini")

def save_configfile():
    global config_file, plat_c, plat_r, col_mask
    with open("config.ini","w") as file_object:
        for key in col_mask:
            config_file["ColMask"][key] = str(col_mask[key])
        config_file["Platform"]["plat_c_x"] = str(plat_c[0])
        config_file["Platform"]["plat_c_y"] = str(plat_c[1])
        config_file["Platform"]["plat_r"] = str(plat_r)
        config_file.write(file_object)

def read_configfile():
    global config_file, plat_c, plat_r, col_mask
    for key in col_mask:
        col_mask[key] = int(config_file["ColMask"][key])
    plat_c[0] = float(config_file["Platform"]["plat_c_x"])
    plat_c[1] = float(config_file["Platform"]["plat_c_y"])
    plat_r = float(config_file["Platform"]["plat_r"])
    time.sleep(0.1)
    print("Settings loaded from config.ini")

def find_ball(img, col_mask):
    # Mask image and return the position and area of the largest contour
    mask, masked_image = mask_img(img, col_mask)
    img_contour, contours = cvzone.findContours(img, mask)
    ball_pos_abs = (0,0)
    ball_area = 0
    img_contour = None
    if contours:
        # Ball found 
        ball_pos_abs = ((contours[0]['center'][0]), (contours[0]['center'][1]))
        ball_area = contours[0]['area']
    return ball_pos_abs, ball_area, img_contour 

def mask_img(img, col_mask):
    # Remove pixels outside mask range
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    min = np.array([col_mask["hmin"], col_mask["smin"], col_mask["vmin"]])
    max = np.array([col_mask["hmax"], col_mask["smax"], col_mask["vmax"]])
    mask = cv2.inRange(hsv, min, max)
    global platform_mask
    mask = cv2.bitwise_and(mask, platform_mask)
    res = cv2.bitwise_and(img, img, mask=mask)
    return mask, res

class FreshestFrame(threading.Thread):
    # From https://gist.github.com/crackwitz/15c3910f243a42dcd9d4a40fcdb24e40
	def __init__(self, capture, name='FreshestFrame'):
		self.capture = capture
		assert self.capture.isOpened()

		# this lets the read() method block until there's a new frame
		self.cond = threading.Condition()

		# this allows us to stop the thread gracefully
		self.running = False

		# keeping the newest frame around
		self.frame = None

		# passing a sequence number allows read() to NOT block
		# if the currently available one is exactly the one you ask for
		self.latestnum = 0

		# this is just for demo purposes		
		self.callback = None
		
		super().__init__(name=name)
		self.start()

	def start(self):
		self.running = True
		super().start()

	def release(self, timeout=None):
		self.running = False
		self.join(timeout=timeout)
		self.capture.release()

	def run(self):
		counter = 0
		while self.running:
			# block for fresh frame
			(rv, img) = self.capture.read()
			assert rv
			counter += 1

			# publish the frame
			with self.cond: # lock the condition for this operation
				self.frame = img if rv else None
				self.latestnum = counter
				self.cond.notify_all()

			if self.callback:
				self.callback(img)

	def read(self, wait=True, seqnumber=None, timeout=None):
		# with no arguments (wait=True), it always blocks for a fresh frame
		# with wait=False it returns the current frame immediately (polling)
		# with a seqnumber, it blocks until that frame is available (or no wait at all)
		# with timeout argument, may return an earlier frame;
		#   may even be (0,None) if nothing received yet

		with self.cond:
			if wait:
				if seqnumber is None:
					seqnumber = self.latestnum+1
				if seqnumber < 1:
					seqnumber = 1
				
				rv = self.cond.wait_for(lambda: self.latestnum >= seqnumber, timeout=timeout)
				if not rv:
					return (self.latestnum, self.frame)

			return (self.latestnum, self.frame)

class CameraHandler(Process):
    def __init__(self, cap_id, queues):
        self.cap_id = cap_id
        self.queues = queues
        super(CameraHandler, self).__init__(target=self.loop)

    def loop(self):
        print("Starting CameraHandler loop")
        self.cap = cv2.VideoCapture(cap_id, cv2.CAP_DSHOW)
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, IMG_W)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, IMG_H)
        self.cap.set(cv2.CAP_PROP_FPS, 30) # Doesn't seem to do anything
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc('M', 'J', 'P', 'G'))
        self.cap.set(cv2.CAP_PROP_EXPOSURE, cam_exposure)
        self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 0)
        self.fresh = FreshestFrame(self.cap)
        # Will try to keep queues full with new frames
        while True:
            retval, img  = self.fresh.read()
            if retval:
                for q in self.queues:
                    if q.qsize() == 0:
                        q.put_nowait(img)


class GUI(Process):
    def __init__(self, image_queue, coord_queue):
        self.image_queue = image_queue
        super(GUI, self).__init__(target=self.loop)
    
    def loop(self):
        print("GUI started")
        read_configfile()
        app = App(self.image_queue)
        app.loop()

class App(tk.Tk):
    # Camera GUI
    def __init__(self, image_queue):
        self.root = tk.Tk()
        self.root.geometry("+0+0") # Open window in top left corner
        self.canv = tk.Canvas(self.root, width=IMG_W, height=IMG_H, borderwidth=0, highlightthickness=0)
        self.image_queue = image_queue
        self.plat_defined = False
        self.color_calibrated = False

    def getImage(self):
        # Fetch camera frame, return both cv2 and tkinter images
        img = self.image_queue.get()
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(img_rgb)
        img_tk = ImageTk.PhotoImage(image=img_pil)
        return img, img_tk

    def cv2_to_tk(self, img):
        # Convert cv2 image format to tkinter
        img_rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
        img_pil = Image.fromarray(img_rgb)
        img_tk = ImageTk.PhotoImage(image=img_pil)
        return img_tk

    def imagePopup(self):
        # Show still image in new window
        popup = tk.Toplevel()
        popup.wm_title("Image")

        cv2Img, tkImg  = self.getImage()
        lbl = tk.Label(popup, image=tkImg)
        lbl.pack()
        while popup.winfo_exists():
            popup.update_idletasks()
            popup.update()
            time.sleep(0.01)

    def center_calib_popup(self):
        # Popup window to pick 3 points, calculate platform center and radius, and store results
        popup = tk.Toplevel()
        canvas = tk.Canvas(popup, width=IMG_W, height=IMG_H, borderwidth=0, highlightthickness=0)
        popup.wm_title("Platform center calibration")
        popup.bind("<Button 1>",self.getorigin)
        global x, y
        calibCoords = []

        cv2Img, tkImg  = self.getImage()
        canvas.create_image((0, 0), image=tkImg, anchor="nw")
        canvas.pack()
        while popup.winfo_exists():
            if x > 0:
                # Click detected, append coords to list and draw
                calibCoords.append([x, y])
                marker_r = 4
                canvas.create_oval(x-marker_r, y-marker_r, x+marker_r, y+marker_r, fill="red")
                x, y = 0, 0
            if len(calibCoords) >= 3:
                # All markers placed
                global plat_c, plat_r
                plat_c, plat_r = self.find_center(calibCoords[0][0], calibCoords[0][1], calibCoords[1][0], calibCoords[1][1], calibCoords[2][0], calibCoords[2][1])
                print("Center at X:{:.2f}; Y:{:.2f}\nRadius:{:.2f}".format(plat_c[0], plat_c[1], plat_r))
                canvas.create_oval(plat_c[0]-plat_r, plat_c[1]-plat_r, plat_c[0]+plat_r, plat_c[1]+plat_r, fill="", outline="blue", width=4)
                popup.update()
                save_configfile() # Store values
                self.plat_defined = False        
                time.sleep(1)
                popup.destroy()

            popup.update_idletasks()
            popup.update()
            time.sleep(0.01)

    def getorigin(self, eventorigin):
        # Gets mouse position
        global x,y
        x = eventorigin.x
        y = eventorigin.y

    def find_center(self, x1, y1, x2, y2, x3, y3):
        # Find circle parameters from 3 points on its circumference
        A = 2 * np.array([[(x2-x1), (y2-y1)],
                        [(x3-x2), (y3-y2)]])
        b = np.array([[x2**2 + y2**2 - x1**2 - y1**2], 
                    [x3**2 + y3**2 - x2**2 - y2**2]])
        center = np.linalg.solve(A, b)
        center = np.squeeze(center)
        r = np.sqrt((x1-center[0])**2 + (y1-center[1])**2)
        return center, r # center[0] = x, center [1] = y

    def color_calib_popup(self):
        # Popup window to calibrate color mask values
        popup = tk.Toplevel()
        canvas = tk.Canvas(popup, width=IMG_W, height=IMG_H, borderwidth=0, highlightthickness=0)
        popup.wm_title("Color calibration")

        cv2Img, tkImg  = self.getImage()
        canv_img = canvas.create_image((0, 0), image=tkImg, anchor="nw")
        canvas.pack(side="left")
        scale_len = 200
        control_frame = tk.Frame(popup); control_frame.pack(side="bottom")
        hue_box = tk.LabelFrame(control_frame, text="Hue")
        sat_box = tk.LabelFrame(control_frame, text="Sat")
        val_box = tk.LabelFrame(control_frame, text="Val")

        global col_mask
        local_mask = {}

        hl = tk.Scale(hue_box, from_=0, to=255, length=scale_len); hl.set(col_mask["hmin"]); hl.pack(side="left")
        hh = tk.Scale(hue_box, from_=0, to=255, length=scale_len); hh.set(col_mask["hmax"]); hh.pack(side="left")
        sl = tk.Scale(sat_box, from_=0, to=255, length=scale_len); sl.set(col_mask["smin"]); sl.pack(side="left")
        sh = tk.Scale(sat_box, from_=0, to=255, length=scale_len); sh.set(col_mask["smax"]); sh.pack(side="left")
        vl = tk.Scale(val_box, from_=0, to=255, length=scale_len); vl.set(col_mask["vmin"]); vl.pack(side="left")
        vh = tk.Scale(val_box, from_=0, to=255, length=scale_len); vh.set(col_mask["vmax"]); vh.pack(side="left")
        
        def read_mask_vals():
            local_mask["hmin"] = hl.get()
            local_mask["hmax"] = hh.get()
            local_mask["smin"] = sl.get()
            local_mask["smax"] = sh.get()
            local_mask["vmin"] = vl.get()
            local_mask["vmax"] = vh.get()

        def save_mask_vals():
            col_mask["hmin"] = hl.get()
            col_mask["hmax"] = hh.get()
            col_mask["smin"] = sl.get()
            col_mask["smax"] = sh.get()
            col_mask["vmin"] = vl.get()
            col_mask["vmax"] = vh.get()
            print("Saving mask:")
            print(col_mask)
            save_configfile() # Store values
            popup.destroy()
            self.color_calibrated = True

        confirm_btn = tk.Button(control_frame, text="Confirm", command=save_mask_vals)
        confirm_btn.pack(side="bottom")
        mask_enabled = tk.BooleanVar(control_frame)
        mask_check = tk.Checkbutton(control_frame, text="Show mask", variable=mask_enabled, offvalue=False, onvalue=True); mask_check.pack(side="bottom")
        val_box.pack(side="bottom"); sat_box.pack(side="bottom"); hue_box.pack(side="bottom")

        while 1:
            cv2Img, tkImg = self.getImage()
            read_mask_vals()
            mask, masked_img = mask_img(cv2Img, local_mask)
            mask_bgr = cv2.cvtColor(mask, cv2.COLOR_GRAY2BGR)
            if mask_enabled.get():
                tkImg = self.cv2_to_tk(mask_bgr)
            else:
                tkImg = self.cv2_to_tk(masked_img)
            canvas.itemconfig(canv_img, image=tkImg)
            popup.update_idletasks()
            popup.update()
            time.sleep(0.01)

    def loop(self):
        #  Initialize image
        print("GUI starting")
        cv2Img, tkImg = self.getImage()
        canv_img = self.canv.create_image((0, 0), image=tkImg, anchor="nw")
        self.canv.pack(side="left")

        btnStill = tk.Button(self.root, text="Get still image", command=self.imagePopup).pack(side="bottom")
        btnCalibrateCenter = tk.Button(self.root, text="Calibrate center", command=self.center_calib_popup).pack(side="bottom")
        btnCalibrateColor = tk.Button(self.root, text="Calibrate color", command=self.color_calib_popup).pack(side="bottom")

        global plat_r, plat_c, col_mask
        self.plat_defined = False
        self.color_calibrated = False

        while 1:
            # Draw new image
            cv2Img, tkImg  = self.getImage()
            self.canv.itemconfig(canv_img, image=tkImg)
            # Draw platform home pos. 
            if plat_r > 0 and not self.plat_defined:
                if "xcoordhelper" in locals():
                    self.canv.delete(xcoordhelper)
                    self.canv.delete(ycoordhelper)
                    self.canv.delete(platcircle)
                xcoordhelper = self.canv.create_line(plat_c[0], plat_c[1], plat_c[0] + 20, plat_c[1], fill="red", width=2)
                ycoordhelper = self.canv.create_line(plat_c[0], plat_c[1], plat_c[0], plat_c[1] + 20, fill="blue", width=2)
                platcircle = self.canv.create_oval(plat_c[0]-plat_r, plat_c[1]-plat_r, plat_c[0]+plat_r, plat_c[1]+plat_r, fill="", outline="blue", width=1)
                self.plat_defined = True

            # Detect contours and draw outline
            if self.color_calibrated:
                ball_pos_abs, ball_area, _ = find_ball(cv2Img, col_mask)
                if "center_circle" in locals():
                    self.canv.delete(center_circle)
                rect_radius = np.sqrt(ball_area/np.pi)
                center_circle = self.canv.create_oval(ball_pos_abs[0]-rect_radius, ball_pos_abs[1]-rect_radius, ball_pos_abs[0]+rect_radius, ball_pos_abs[1]+rect_radius, fill="", outline="green", width=4)

            # Update GUI
            self.root.update_idletasks()
            self.root.update()


class BallTracker(Process):
    # Tracks the largest contour visible after masking
    def __init__(self, image_queue, coord_queues):
        self.image_queue = image_queue
        self.coord_queues = coord_queues
        super(BallTracker, self).__init__(target=self.loop)

    def loop(self):
        # Get ball position as fast as images are captured, and output coordinates
        print("Ball tracker started")
        global col_mask
        while True:
            img = self.image_queue.get()
            ball_pos, ball_area, _ = find_ball(img, col_mask) # TODO: Will not get updated unless application is closed
            for q in self.coord_queues:
                if q.qsize() == 0:
                    q.put_nowait(ball_pos)



read_configfile()

# Mask out area outside platform circle
platform_mask = np.zeros((IMG_H, IMG_W), np.uint8)
platform_mask = cv2.circle(platform_mask, center=(round(plat_c[0]), round(plat_c[1])), radius=round(plat_r * 0.8), thickness=-1, color=255)

if __name__ == '__main__':
    coord_queue_gui = Queue(maxsize=1) # Communication between the two processes - ball position coordinates
    image_queue_gui = Queue(maxsize=1)
    image_queue_tracker = Queue(maxsize=1)

    camera = CameraHandler(cap_id, [image_queue_gui, image_queue_tracker])
    balltracker = BallTracker(image_queue_tracker, [coord_queue_gui])
    gui = GUI(image_queue_gui, coord_queue_gui)

    camera.start()
    gui.start()
    balltracker.start()

    gui.join()
    camera.kill()
    balltracker.kill()