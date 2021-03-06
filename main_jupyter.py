"""People Counter."""

"""
 Copyright (c) 2018 Intel Corporation.
 Permission is hereby granted, free of charge, to any person obtaining
 a copy of this software and associated documentation files (the
 "Software"), to deal in the Software without restriction, including
 without limitation the rights to use, copy, modify, merge, publish,
 distribute, sublicense, and/or sell copies of the Software, and to
 permit person to whom the Software is furnished to do so, subject to
 the following conditions:
 The above copyright notice and this permission notice shall be
 included in all copies or substantial portions of the Software.
 THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
 EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
 NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
 LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
 OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
 WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
"""


import os
import sys
import time
import socket
import json
import cv2
import subprocess
import logging as log
import paho.mqtt.client as mqtt

from inference import Network

# MQTT server environment variables
HOSTNAME = socket.gethostname()
IPADDRESS = socket.gethostbyname(HOSTNAME)
MQTT_HOST = IPADDRESS
MQTT_PORT = 1884
MQTT_KEEPALIVE_INTERVAL = 60

try:
    # Probability threshold for detections filtering
    prob_threshold = float(os.environ['PROB_THRESHOLD'])
except:
    prob_threshold = 0.5


def performance_counts(perf_count):
    """
    print information about layers of the network.

    :param perf_count: Dictionary describing the status of the layers
    :return: None
    """
    print("{:<70} {:<15} {:<15} {:<15} {:<10}".format('name', 'layer_type',
                                                      'exec_type', 'status',
                                                      'real_time, us'))
    for layer, stats in perf_count.items():
        print("{:<70} {:<15} {:<15} {:<15} {:<10}".format(layer,
                                                          stats['layer_type'],
                                                          stats['exec_type'],
                                                          stats['status'],
                                                          stats['real_time']))


def ssd_parser(frame, result):
    """
    parses the ssd output

    :param frame: frame from camera/video
    :param result: list contains the data to parse ssd
    :return: person count and frame
    """
    current_count = 0
    for obj in result[0][0]:
        # Draw bounding box for object when it's probability is more
        # than the specified threshold
        if float(obj[2]) > float(prob_threshold):
            xmin = int(obj[3] * initial_w)
            ymin = int(obj[4] * initial_h)
            xmax = int(obj[5] * initial_w)
            ymax = int(obj[6] * initial_h)
            cv2.rectangle(frame, (xmin, ymin), (xmax, ymax),
                          (0, 55, 255), 1)
            current_count = current_count + 1
    return frame, current_count


def main():
    """
    Load the network and parse the SSD output.

    :return: None
   """
    # Connect to the MQTT server
    client = mqtt.Client()
    client.connect(MQTT_HOST, MQTT_PORT, MQTT_KEEPALIVE_INTERVAL)

    log.basicConfig(format="[ %(levelname)s ] %(message)s", level=log.INFO,
                    stream=sys.stdout)

    # Flag for the input image
    single_image_mode = False

    cur_request_id = 0
    last_count = 0
    total_count = 0
    start_time = 0

    model = os.environ['MODEL']
    device = os.environ['DEVICE'] if 'DEVICE' in os.environ.keys() else 'CPU'
    cpu_extension = os.environ[
        'CPU_EXTENSION'] if 'CPU_EXTENSION' in os.environ.keys() else None

    # Checks for live feed
    if os.environ['INPUT'] == 'CAM':
        input_stream = 0

    # Checks for input image
    elif os.environ['INPUT'].endswith('.jpg') or os.environ['INPUT'].endswith('.bmp'):
        single_image_mode = True
        input_stream = os.environ['INPUT']

    # Checks for video file
    else:
        input_stream = os.environ['INPUT']
        assert os.path.isfile(os.environ['INPUT']), "Specified input file doesn't exist"

    cap = cv2.VideoCapture(input_stream)

    if input_stream:
        cap.open(os.environ['INPUT'])

    if not cap.isOpened():
        log.error("ERROR! Unable to open video source")
    # Initialise the class
    infer_network = Network()
    # Load the network to IE plugin to get shape of input layer
    n, c, h, w = infer_network.load_model(model, device, 1, 1,
                                          cur_request_id, cpu_extension)[1]
    global initial_w,initial_h
    initial_w = cap.get(3)
    initial_h = cap.get(4)
    fps = cap.get(cv2.CAP_PROP_FPS)
    cmdstring = ('ffmpeg',
                 '-y', '-r', '%d' %(fps), # overwrite, 60fps
                 '-s', '%dx%d' % (initial_w, initial_h), # size of image string
                 '-pixel_format' ,  'bgr24', # format
                 '-f', 'rawvideo',  '-i', '-', # tell ffmpeg to expect raw video from the pipe
                 'http://localhost:8090/fac.ffm') # output encoding
    p = subprocess.Popen(cmdstring, stdin=subprocess.PIPE)
    while cap.isOpened():
        flag, frame = cap.read()
        if not flag:
            break
        key_pressed = cv2.waitKey(60)
        # Start async inference
        inf_start = time.time()
        image = cv2.resize(frame, (w, h))
        # Change data layout from HWC to CHW
        image = image.transpose((2, 0, 1))
        image = image.reshape((n, c, h, w))
        # Start asynchronous inference for specified request.
        infer_network.exec_net(cur_request_id, image)
        # Wait for the result
        if infer_network.wait(cur_request_id) == 0:
            det_time = time.time() - inf_start
            # Results of the output layer of the network
            result = infer_network.get_output(cur_request_id)
            if os.environ['PERF_COUNTS'] > str(0):
                perf_count = infer_network.performance_counter(cur_request_id)
                performance_counts(perf_count)
            frame, current_count = ssd_parser(frame, result)
            inf_time_message = "Inference time: {:.3f}ms" \
                .format(det_time * 1000)
            cv2.putText(frame, inf_time_message, (15, 15),
                        cv2.FONT_HERSHEY_COMPLEX, 0.5, (200, 10, 10), 1)

            # When new person enters the video
            if current_count > last_count:
                start_time = time.time()
                total_count = total_count + current_count - last_count
                client.publish("person", json.dumps({"total": total_count}))

            # Person duration in the video is calculated
            if current_count < last_count:
                duration = int(time.time() - start_time)
                # Publish messages to the MQTT server
                client.publish("person/duration",
                               json.dumps({"duration": duration}))

            client.publish("person", json.dumps({"count": current_count}))
            last_count = current_count

            if key_pressed == 27:
                break

        p.stdin.write(frame.tostring())
        if single_image_mode:
            cv2.imwrite('output_image.jpg', frame)
            infer_network.clean()
    cap.release()
    cv2.destroyAllWindows()
    client.disconnect()
    infer_network.clean()

if __name__ == '__main__':
    main()
    exit(0)


