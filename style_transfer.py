#!/usr/bin/env python3

"""Neural style transfer using Caffe. Implements http://arxiv.org/abs/1508.06576."""

# pylint: disable=invalid-name, too-many-arguments, too-many-locals

import argparse
from fractions import Fraction
from http.server import BaseHTTPRequestHandler, HTTPServer
import io
import os
from socketserver import ThreadingMixIn
import sys
import threading
import time
import webbrowser

import numpy as np
from PIL import Image
from scipy.ndimage import convolve

os.environ['GLOG_minloglevel'] = '2'
import caffe  # pylint: disable=wrong-import-position

# Machine epsilon for float32
EPS = np.finfo(np.float32).eps


def normalize(arr):
    """Normalize an array such that its quadratic mean (RMS) is 1."""
    rms = np.sqrt(np.mean(arr*arr))
    if rms <= 0:
        rms = 1
    return arr / rms


def gram_matrix(feat):
    n, mh, mw = feat.shape
    feat = feat.reshape((n, mh * mw))
    return feat @ feat.T


class LayerIndexer:
    def __init__(self, net, attr):
        self.net, self.attr = net, attr

    def __getitem__(self, key):
        return getattr(self.net.blobs[key], self.attr)[0]

    def __setitem__(self, key, value):
        getattr(self.net.blobs[key], self.attr)[0] = value


class CaffeModel:
    def __init__(self, deploy, weights, mean=(0, 0, 0), bgr=True):
        self.mean = np.float32(mean)[..., None, None]
        assert self.mean.ndim == 3
        self.bgr = bgr
        self.net = caffe.Net(deploy, 1, weights=weights)
        self.data = LayerIndexer(self.net, 'data')
        self.diff = LayerIndexer(self.net, 'diff')

    def get_image(self):
        arr = self.data['data'] + self.mean
        if self.bgr:
            arr = arr[::-1]
        arr = arr.transpose((1, 2, 0))
        return Image.fromarray(np.uint8(np.clip(arr, 0, 255)))

    def set_image(self, img):
        arr = np.float32(img).transpose((2, 0, 1))
        if self.bgr:
            arr = arr[::-1]
        self.net.blobs['data'].reshape(1, 3, *arr.shape[-2:])
        self.data['data'] = arr - self.mean

    def layers(self):
        """Returns the layer names of the network."""
        layers = []
        for i, layer in enumerate(self.net.blobs.keys()):
            if i == 0:
                continue
            if layer.find('_split_') == -1:
                layers.append(layer)
        return layers

    def transfer(self, iterations, content_image, style_image, content_layers, style_layers,
                 step_size=1, content_weight=1, style_weight=1, tv_weight=0, callback=None):
        b1, b2 = 0.9, 0.9

        content_weight /= max(len(content_layers), 1)
        style_weight /= max(len(style_layers), 1)

        # Construct list of layers to visit during the backward pass
        layers = []
        for layer in reversed(self.layers()):
            if layer in content_layers or layer in style_layers:
                layers.append(layer)

        # Prepare feature maps from content image
        features = {}
        self.set_image(content_image)
        self.net.forward(end=layers[0])
        for layer in content_layers:
            features[layer] = self.data[layer].copy()

        # Prepare Gram matrices from style image
        grams = {}
        self.set_image(style_image)
        self.net.forward(end=layers[0])
        for layer in style_layers:
            grams[layer] = gram_matrix(self.data[layer])

        # Initialize the model with a white noise image
        w, h = content_image.size
        self.set_image(np.random.uniform(0, 255, (h, w, 3)))
        m1 = np.zeros((3, h, w), dtype=np.float32)
        m2 = np.zeros((3, h, w), dtype=np.float32)

        for step in range(1, iterations+1):
            # Prepare gradient buffers and run the model forward
            for layer in layers:
                self.diff[layer] = 0
            self.net.forward(end=layers[0])

            for i, layer in enumerate(layers):
                # Compute the content and style gradients
                if layer in content_layers:
                    c_grad = self.data[layer] - features[layer]
                    self.diff[layer] += normalize(c_grad)*content_weight
                if layer in style_layers:
                    current_gram = gram_matrix(self.data[layer])
                    c = 1 / self.data[layer].size**2
                    n, mh, mw = self.data[layer].shape
                    feat = self.data[layer].reshape((n, mh * mw))
                    s_grad = c * (feat.T @ (current_gram - grams[layer])).T
                    s_grad = s_grad.reshape((n, mh, mw))
                    self.diff[layer] += normalize(s_grad)*style_weight

                # Run the model backward
                if i+1 == len(layers):
                    self.net.backward(start=layer)
                else:
                    self.net.backward(start=layer, end=layers[i+1])

            # Compute total variation gradient
            tv_kernel = np.float32([[[0, -1, 0], [-1, 4, -1], [0, -1, 0]]])
            tv = convolve(self.data['data'], tv_kernel)

            # Compute a weighted sum of normalized gradients
            update = normalize(self.diff['data']) + tv_weight*normalize(tv)

            # Adam update
            m1 = b1*m1 + (1-b1)*update
            m2 = b2*m2 + (1-b2)*update*update
            self.data['data'] -= step_size * m1/(1-b1**step) / (np.sqrt(m2/(1-b2**step)) + EPS)
            # self.data['data'] -= step_size * update
            if callback is not None:
                callback(step=step)

        return self.get_image()


class Progress:
    prev_t = None
    step = None

    def __init__(self, model, url=None, steps=-1):
        self.model = model
        self.url = url
        self.steps = steps

    def __call__(self, step=None):
        this_t = time.perf_counter()
        self.step = step
        if step == 1:
            print('Step %d' % step, flush=True)
            if self.url:
                webbrowser.open(self.url)
        else:
            print('Step %d, time: %.2f s' % (step, this_t-self.prev_t), flush=True)
        self.prev_t = this_t


class ProgressServer(ThreadingMixIn, HTTPServer):
    model = None
    progress = None


class ProgressHandler(BaseHTTPRequestHandler):
    index = """
    <meta http-equiv="refresh" content="5">
    <h1>style_transfer</h1>
    <img src="/out.png">
    <p>Step %(step)d/%(steps)d
    """

    def do_GET(self):
        if self.path == '/':
            self.send_response(200)
            self.send_header('Content-type', 'text/html')
            self.end_headers()
            self.wfile.write((self.index % {
                'step': self.server.progress.step,
                'steps': self.server.progress.steps,
            }).encode())
        elif self.path == '/out.png':
            self.send_response(200)
            self.send_header('Content-type', 'image/png')
            self.end_headers()
            buf = io.BytesIO()
            self.server.model.get_image().save(buf, format='png')
            self.wfile.write(buf.getvalue())
        else:
            self.send_error(404)

def ffloat(s):
    return float(Fraction(s))


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('content_image', help='the content image')
    parser.add_argument('style_image', help='the style image')
    parser.add_argument('output_image', nargs='?', default='out.png', help='the output image')
    parser.add_argument(
        '-i', dest='iterations', type=int, default=500, help='the number of iterations')
    parser.add_argument(
        '-s', dest='step_size', type=ffloat, default=10,
        help='the step size (iteration strength)')
    parser.add_argument('--size', type=int, default=224, help='the maximum output size')
    parser.add_argument('--style-size', type=int, default=224, help='the style size')
    parser.add_argument(
        '-cw', dest='content_weight', type=ffloat, default=0.1, help='the content image factor')
    parser.add_argument(
        '-tw', dest='tv_weight', type=ffloat, default=0.1, help='the smoothing factor')
    parser.add_argument(
        '--content-layers', nargs='*', default=['conv4_2'], metavar='LAYER',
        help='the layers to use for content')
    parser.add_argument(
        '--style-layers', nargs='*', metavar='LAYER',
        default=['conv1_1', 'conv2_1', 'conv3_1', 'conv4_1', 'conv5_1'],
        help='the layers to use for style')
    parser.add_argument(
        '-p', dest='port', type=int, default=8000, help='the port to use for the http server')
    parser.add_argument('--no-browser', action='store_true', help='don\'t open a web browser')
    return parser.parse_args()


def main():
    args = parse_args()
    content_image = Image.open(args.content_image).convert('RGB')
    style_image = Image.open(args.style_image).convert('RGB')
    content_image = content_image.resize((args.size, args.size), Image.LANCZOS)
    style_image = style_image.resize((args.style_size, args.style_size), Image.LANCZOS)

    model = CaffeModel('VGG_ILSVRC_19_layers_deploy.prototxt',
                       'VGG_ILSVRC_19_layers.caffemodel',
                       (103.939, 116.779, 123.68))

    server_address = ('', args.port)
    url = 'http://127.0.0.1:%d/' % args.port
    server = ProgressServer(server_address, ProgressHandler)
    server.model = model
    progress_args = {}
    if not args.no_browser:
        progress_args['url'] = url
    server.progress = Progress(model, steps=args.iterations, **progress_args)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print('\nWatch the progress at: %s\n' % url)

    caffe.set_mode_gpu()  # FIXME: add an argument
    caffe.set_random_seed(0)
    np.random.seed(0)
    try:
        output_image = model.transfer(
            args.iterations, content_image, style_image, args.content_layers, args.style_layers,
            step_size=args.step_size, content_weight=args.content_weight, tv_weight=args.tv_weight,
            callback=server.progress)
    except KeyboardInterrupt:
        output_image = model.get_image()
    print('Saving output as %s.' % args.output_image)
    output_image.save(args.output_image)

if __name__ == '__main__':
    main()
