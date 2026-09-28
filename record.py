# This script is installed on the nucs that are connected via usb with the IDS cameras.
# You can specify how long you want to record and the exposure time in ms. We found 15ms best for our setting.
# It creates in the specified dir a folder frames with all images and the file frames.csv, containing the image name and the timestamp.
# e.g. python3 record.py recordings/test 60 15 --> creates folder recordings/test and starts a 60s recording with 15ms exposure time.

# This script is partly written with the help of AI.

import os
import time
import argparse
import queue
import threading

# path to the IDS peak driver, otherwise the camera is not found
os.environ.setdefault("GENICAM_GENTL64_PATH", "/usr/lib/x86_64-linux-gnu/ids-peak/cti")

import cv2
from ids_peak import ids_peak, ids_peak_ipl_extension


NUM_BUFFERS = 16  # number of image buffers of the camera
NUM_WRITERS = 4   # number of threads that save the images as png


def execute_command(nodemap, name):
    '''
    Executes a command of the camera (e.g. AcquisitionStart) and waits until it is done.

    :param nodemap: nodemap of the camera
    :param name: name of the command
    '''
    node = nodemap.FindNode(name)
    node.Execute()
    node.WaitUntilDone()


def open_camera(exposure_ms):
    '''
    Opens the first camera connected to the nuc and sets it to hardware trigger mode (Line0, rising edge).

    :param exposure_ms: exposure time in ms
    :return: device, nodemap, datastream
    '''
    device_manager = ids_peak.DeviceManager.Instance()
    device_manager.Update()
    if len(device_manager.Devices()) == 0:
        raise Exception("No IDS camera found. Is the camera connected via usb?")

    device = device_manager.Devices()[0].OpenDevice(ids_peak.DeviceAccessType_Control)
    nodemap = device.RemoteDevice().NodeMaps()[0]

    # load factory defaults, so every recording uses the same settings
    nodemap.FindNode("UserSetSelector").SetCurrentEntry("Default")
    execute_command(nodemap, "UserSetLoad")

    # grayscale images
    nodemap.FindNode("PixelFormat").SetCurrentEntry("Mono8")

    # exposure time has to be given in microseconds
    nodemap.FindNode("ExposureTime").SetValue(exposure_ms * 1000)

    # frame rate limit as high as possible, otherwise trigger pulses get skipped
    fps_node = nodemap.FindNode("AcquisitionFrameRate")
    fps_node.SetValue(fps_node.Maximum())

    # hardware trigger
    nodemap.FindNode("TriggerMode").SetCurrentEntry("On")
    nodemap.FindNode("TriggerSource").SetCurrentEntry("Line0")
    nodemap.FindNode("TriggerActivation").SetCurrentEntry("RisingEdge")

    # buffers the camera writes the images into
    datastream = device.DataStreams()[0].OpenDataStream()
    payload_size = nodemap.FindNode("PayloadSize").Value()
    num_buffers = max(NUM_BUFFERS, datastream.NumBuffersAnnouncedMinRequired())
    for i in range(num_buffers):
        buffer = datastream.AllocAndAnnounceBuffer(payload_size)
        datastream.QueueBuffer(buffer)

    return device, nodemap, datastream


def close_camera(nodemap, datastream):
    '''
    Stops the acquisition, frees the buffers and turns the trigger mode off again.

    :param nodemap: nodemap of the camera
    :param datastream: datastream of the camera
    '''
    execute_command(nodemap, "AcquisitionStop")
    datastream.StopAcquisition(ids_peak.AcquisitionStopMode_Default)
    datastream.Flush(ids_peak.DataStreamFlushMode_DiscardAll)
    for buffer in datastream.AnnouncedBuffers():
        datastream.RevokeBuffer(buffer)

    nodemap.FindNode("TLParamsLocked").SetValue(0)
    nodemap.FindNode("TriggerMode").SetCurrentEntry("Off")


def save_images(image_queue):
    '''
    Runs in its own thread. Takes (path, image) from the queue and saves the image.
    Stops if it gets None.

    :param image_queue: queue with (path, image)
    '''
    while True:
        job = image_queue.get()
        if job is None:
            break
        path, img = job



        
        cv2.imwrite(path, img, [cv2.IMWRITE_PNG_COMPRESSION, 1])


def record(out_dir, duration, exposure_ms):
    '''
    Records images for duration seconds. Every trigger pulse = one image.
    Images are saved in out_dir/frames/000000.png, 000001.png, ...
    frames.csv contains for every image the name and the time (system clock of the nuc) when it arrived.

    :param out_dir: output folder (e.g. recordings/test)
    :param duration: recording time in seconds
    :param exposure_ms: exposure time in ms
    '''
    print('------------------ RECORDING -------------------')

    frames_dir = os.path.join(out_dir, "frames")
    if os.path.exists(frames_dir):
        raise Exception(f"{frames_dir} already exists. Choose another name or delete the folder.")
    os.makedirs(frames_dir)

    device, nodemap, datastream = open_camera(exposure_ms)

    # png encoding takes time --> multiple therads
    image_queue = queue.Queue()
    writers = []
    for i in range(NUM_WRITERS):
        t = threading.Thread(target=save_images, args=(image_queue,))
        t.start()
        writers.append(t)

    
    nodemap.FindNode("TLParamsLocked").SetValue(1)
    datastream.StartAcquisition()
    execute_command(nodemap, "AcquisitionStart")
    print(f"Recording {duration:g} s with exposure time {exposure_ms:g} ms")

    rows = []  # (image name, timestamp)
    frame_idx = 0
    num_incomplete = 0
    # monotonic clock for the duration, so a ntp correction during the recording does not change the length
    start_time = time.monotonic()
    end_time = start_time + duration

    try:
        while time.monotonic() < end_time:
            # wait max. 500 ms for the next image
            try:
                buffer = datastream.WaitForFinishedBuffer(500)
            except ids_peak.TimeoutException:
                continue

            # timestamp directly after the image arrived
            timestamp = time.time_ns()

            if buffer.IsIncomplete():
                num_incomplete += 1
            else:
                img = ids_peak_ipl_extension.BufferToImage(buffer)
                # copy, because the SDK reuses the buffer memory
                frame = img.get_numpy_1D().reshape(img.Height(), img.Width()).copy()

                img_name = f"frames/{frame_idx:06d}.png"
                image_queue.put((os.path.join(out_dir, img_name), frame))
                rows.append((img_name, timestamp))
                frame_idx += 1

            # give buffer back to the camera
            datastream.QueueBuffer(buffer)

    except KeyboardInterrupt:
        print("Recording stopped")

    record_time = time.monotonic() - start_time
    close_camera(nodemap, datastream)

    # stop the writer threads (one None for every thread) and wait until all images are saved
    print("Saving remaining images ...")
    for i in range(NUM_WRITERS):
        image_queue.put(None)
    for t in writers:
        t.join()

    # write frames.csv
    csv_path = os.path.join(out_dir, "frames.csv")
    with open(csv_path, "w") as f:
        f.write("file,host_time_ns\n")
        for img_name, timestamp in rows:
            f.write(f"{img_name},{timestamp}\n")

    print(f"Recorded {len(rows)} frames in {record_time:.1f} seconds ({len(rows) / record_time:.1f} fps).")
    if num_incomplete > 0:
        print(f"Warning: {num_incomplete} incomplete frames were skipped.")
   
    print('------------------------------------------------')


def main():
    parser = argparse.ArgumentParser(description="Record frames with hardware trigger")
    parser.add_argument("out_dir", type=str, help="output folder, e.g. recordings/test")
    parser.add_argument("duration", type=float, help="recording time in seconds")
    parser.add_argument("exposure_ms", type=float, help="exposure time in ms (we use 15)")
    args = parser.parse_args()

    ids_peak.Library.Initialize()
    record(args.out_dir, args.duration, args.exposure_ms)
    ids_peak.Library.Close()


# python3 record.py recordings/test 60 15

if __name__ == "__main__":
    main()