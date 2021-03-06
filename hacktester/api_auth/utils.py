import hashlib
import random


def keys_not_present(keys, d):
    return [k for k in keys if k not in d]


def generate_random_key(host, algo='sha256'):
    random_bits = str(random.getrandbits(256)) + host
    h = hashlib.new(algo)
    h.update(random_bits.encode('utf-8'))
    return h.hexdigest()
