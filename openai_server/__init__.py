import os
import sys
import tqdm
import traceback
import time
import base64
import secrets
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gpt'))
from json import loads, dumps
from json import load as json_load

from sanic import Sanic
from sanic.response import json, text
from sanic_cors import CORS, cross_origin

from pprint import pprint as pp

from openai_server.gpt import sample, model, encoder

import tensorflow.compat.v1 as tf
import ftfy

from tokenizers import Tokenizer
from transformers import GPT2TokenizerFast


class GPTEngine:
  def __init__(self, api, model_name, batch_size=1):
    self.api = api
    self.id = model_name
    self.ckpt = tf.train.latest_checkpoint(os.path.join(api.model_path, model_name))
    if self.ckpt is None:
      raise ValueError("Couldn't load checkpoint for {model_name} from {path}".format(model_name=model_name, path=os.path.join(api.model_path, model_name)))
    self.graph = tf.Graph()
    self.config = tf.ConfigProto(allow_soft_placement=True, log_device_placement=True)
    self.session = tf.Session(graph=self.graph, config=self.config)
    #self.encoder = encoder.get_encoder(model_name, self.api.model_path)
    self.encoder = GPT2TokenizerFast.from_pretrained("gpt2")
    self.hparams = model.default_hparams()
    with open(os.path.join(self.api.model_path, model_name, 'hparams.json')) as f:
      params = json_load(f)
      self.hparams.override_from_dict(params)
    with self.session.as_default() as sess, self.graph.as_default() as graph:
      pp(self.session.list_devices())
      if 'CUDA_VISIBLE_DEVICES' in os.environ:
        print('Using /gpu:0 on device {}'.format(os.environ['CUDA_VISIBLE_DEVICES']))
      with tf.device('/gpu:0' if 'CUDA_VISIBLE_DEVICES' in os.environ else None):
        self.batch_size = batch_size
        self.context = tf.placeholder(tf.int32, [self.batch_size, None], name="context")
        self.length = tf.placeholder(tf.int32, (), name="length")
        self.temperature = tf.placeholder(tf.float32, (), name="temperature")
        self.top_k = tf.placeholder(tf.int32, (), name="top_k")
        self.top_p = tf.placeholder(tf.float32, (), name="top_p")
        self.frequency_penalty = tf.placeholder(tf.float32, (), name="frequency_penalty")
        #np.random.seed(seed)
        #tf.set_random_seed(seed)
        self.output = sample.sample_sequence(
            hparams=self.hparams,
            length=self.length,
            context=self.context,
            batch_size=self.batch_size,
            temperature=self.temperature,
            top_k=self.top_k,
            top_p=self.top_p,
            frequency_penalty=self.frequency_penalty,
        )
      var_list = tf.trainable_variables()
      self.saver = tf.train.Saver(var_list=var_list)
      for v in var_list:
        print(self.ckpt, v)
      pp(self.hparams)
      print('Restoring from {!r}'.format(self.ckpt))
      self.saver.restore(sess, self.ckpt)


  def fix(self, text):
    fixed = ftfy.fix_text(text)
    return fixed


  # GPT2Tokenizer and Tokenizer has different ways of fetching token ids
  def encode(self, text, encoder=None):
    if encoder is None:
      encoder = self.encoder
    result = encoder.encode(text)
    if isinstance(result, list):
        return result
    return result.ids

  def stop_text_1(self, stop, completion_text):
    if stop is None or len(stop) <= 0:
      return completion_text
    if completion_text.startswith(stop):
      return stop + self.stop_text_1(stop, completion_text[len(stop):])
    return completion_text.split(stop, 1)[0]

  def stop_text(self, stops, completion_text):
    if stops is None:
      return False, completion_text
    if not isinstance(stops, (list, tuple)):
      stops = [stops]
    original_text = completion_text
    for stop in stops:
      completion_text = self.stop_text_1(stop, completion_text)
    return original_text != completion_text, completion_text


  def completion(self, prompt, n=None, max_tokens=None, logprobs=None, stream=False, temperature=None, top_p=None, top_k=None, echo=None, frequency_penalty=None, best_of=None, stop=None, **kws):
    if temperature is None:
      temperature = 0.9
    if top_p is None:
      top_p = 1.0
    if top_k is None:
      top_k = 0
    if max_tokens is None:
      max_tokens = 16
    if max_tokens > int(os.environ.get('MAX_TOKENS', '500')):
      max_tokens = int(os.environ.get('MAX_TOKENS', '500'))
    if n is None:
      n = 1
    if n >= int(os.environ.get('MAX_N', '4')):
      n = int(os.environ.get('MAX_N', '4')) # cap to 4 choices
    if echo is None:
      echo = False
    if frequency_penalty is None or frequency_penalty <= 0.0:
      frequency_penalty = 1.0
    if stop is not None:
      if isinstance(stop, str):
        stop = [stop]
      print('Stop: {!r}'.format(stop))
    if len(kws) > 0:
      print('Got extra keywords: {!r}'.format(kws))
    prompts = prompt if isinstance(prompt, (list, tuple)) else [prompt]
    prompts = list(prompts)
    for prompt in prompts:
      prompt = self.fix(prompt)
      with self.session.as_default() as sess, self.graph.as_default() as graph:
        tokens = self.encode(prompt)
        while len(tokens) + max_tokens >= self.hparams.n_ctx:
          tokens = tokens[1:]
        length = max_tokens
        for i in range(n):
          params = {
            self.temperature: temperature,
            self.top_p: top_p,
            self.top_k: top_k,
            self.frequency_penalty: frequency_penalty,
            self.length: length,
          }
          print(params)
          result = self.session.run(self.output, {self.context: [tokens], **params})
          result_tokens = result[0]
          completion = result_tokens[len(tokens):]
          completion_text = self.encoder.decode(completion)
          print('completion_text = {!r}'.format(completion_text))
          finish_reason = 'length'
          stopped, completion_text = self.stop_text(stop, completion_text)
          if stopped:
            finish_reason = 'stop'
          if echo:
            text = prompt + completion_text
          else:
            text = completion_text
          print(repr(text))
          yield {'index': i, 'logprobs': None, 'text': text, 'finish-reason': finish_reason}

class API:
  def __init__(self, model_path=None):
    if model_path is None:
      model_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'models')
    self.model_path = model_path
    self.models = []
    self.engines = {}
    if 'MODELS' in os.environ:
      models = os.environ['MODELS'].split(',')
    else:
      models = os.listdir(self.model_path)
    for model in tqdm.tqdm(models):
      try:
        engine = GPTEngine(api=self, model_name=model)
        self.engines[model] = engine
        self.models.append(model)
      except:
        traceback.print_exc()
    pp(self.engines)
    pp(self.models)


  def engines_list(self):
    for model in self.models:
      yield {
          "id": model,
          "object": "engine",
          "owner": "openai",
          "ready": True,
      }

api = API()

app = Sanic(name='openai-server')
CORS(app)


def log_request(request):
  #import pdb; pdb.set_trace()
  headers = dict(list(request.headers.items()))
  if 'authorization' in headers:
    del headers['authorization']
  headers['x-openai-client-user-agent'] = loads(headers.get('x-openai-client-user-agent', '{}'))
  props = {}
  props['url'] = request.url
  props['method'] = request.method
  props['headers'] = headers
  props['request'] = request.json
  pp(props)
  #print(request.json)


@app.route('/v1/engines')
async def v1_engines_list(request):
  log_request(request)
  res = {"object": "list", "data": []}
  for result in api.engines_list():
    res["data"].append(result)
  return json(res)
  return json({
    "data": [
      {
        "id": "ada",
        "object": "engine",
        "owner": "openai",
        "ready": True
      },
      {
        "id": "ada-beta",
        "object": "engine",
        "owner": "openai",
        "ready": True
      },
      {
        "id": "babbage",
        "object": "engine",
        "owner": "openai",
        "ready": True
      },
      {
        "id": "babbage-beta",
        "object": "engine",
        "owner": "openai",
        "ready": True
      },
      {
        "id": "curie",
        "object": "engine",
        "owner": "openai",
        "ready": True
      },
      {
        "id": "curie-beta",
        "object": "engine",
        "owner": "openai",
        "ready": True
      },
      {
        "id": "davinci",
        "object": "engine",
        "owner": "openai",
        "ready": True
      },
      {
        "id": "davinci-beta",
        "object": "engine",
        "owner": "openai",
        "ready": True
      }
    ],
    "object": "list"
  })


def random_id(prefix, nbytes=18):
  token = secrets.token_bytes(nbytes)
  return prefix + '-' + base64.urlsafe_b64encode(token).decode('utf8')


def number(x):
  if isinstance(x, str):
    try:
      x = int(x)
    except ValueError:
      try:
        x = float(x)
      except ValueError:
        pass
  return x


def json_pretty_dumps(x):
   return dumps(x, sort_keys=True, indent=2)


from urllib import parse

@app.route('/v1/engines/<engine_name>/completions', methods=['POST', 'GET'])
async def v1_engines_completions(request, engine_name):
  log_request(request)
  kws = request.json
  if kws is None:
    url, query = request.url.split('?', 1) if '?' in request.url else (request.url, '')
    kws = dict(parse.parse_qsl(query))
    kws = {k: number(v) for k, v in kws.items()}
  pp(kws)
  engine = None
  if engine_name in api.engines:
    engine = api.engines[engine_name]
  else:
    # rather than throw an error when someone attempts to use an
    # invalid engine, silently fall back to any valid engine for
    # simplicity. E.g. if they try to request 'davinci' but you're
    # serving 117M, then automatically fall back to 117M.
    for info in api.engines_list():
      print('Warning: attempted to use invalid enngine {!r}; falling back to engine {!r}'.format(engine_name, info['id']))
      engine = api.engines[info['id']]
      break
  if engine is None:
    raise RuntimeError("Not serving any models. Try running `python3 download_model.py 117M` and be sure to `export MODELS=117M` before starting the server.")
      
  choices = []
  for choice in engine.completion(**kws):
    choices.append(choice)
  id_ = random_id("cmpl")
  return json({"id": id_, "object": "text_completion", "created": time.time(), "model": engine.id, "choices": choices}, dumps=json_pretty_dumps)
  #return json({"id": "cmpl-Wt5z1RZglyDHHl0SnSvKWVzA", "object": "text_completion", "created": 1599616871, "model": "davinci:2020-05-03", "choices": [{"text": "Test.SetLayerPropertiesWithNonContainedInvisible (", "index": 0, "logprobs": None, "finish_reason": "length"}]})
    
if __name__ == '__main__':
  tf.enable_v2_behavior()
  tf.enable_resource_variables()
  from tensorflow.python.framework.ops import disable_eager_execution
  disable_eager_execution()
  args = sys.argv[1:]
  port = int(args[0] if len(args) > 0 else os.environ.get('PORT', '9000'))
  app.run(host='0.0.0.0', port=port, debug=True)

