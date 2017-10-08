# -*- coding: utf-8 -*-
"""
pytube.cipher
~~~~~~~~~~~~~

YouTube's strategy to restrict downloading videos is to send a ciphered version
of the signature to the client, along with the decryption algorithm obfuscated
in JavaScript. For the clients to play the videos, JavaScript must take the
ciphered version, pass it through a series of "transform functions," and then
signs the media URL with the output. On the backend, they verify that the
signature sent in the GET parameters is valid, and then returns the content
(video/audio stream) or 403 unauthorized accordingly.

This module is responsible for (1) finding and extracting those "transform
functions" (2) maps them to Python equivalents and (3) taking the ciphered
signature and decoding it.
"""
from __future__ import absolute_import

import logging
import pprint
import re
from itertools import chain

from pytube.exceptions import RegexMatchError
from pytube.helpers import memoize
from pytube.helpers import regex_search


logger = logging.getLogger(__name__)


def get_initial_function_name(js):
    """Extracts the name of the function responsible for computing the signature.

    :param str js:
        The contents of the base.js asset file.

    """
    # c&&d.set("signature", EE(c));
    pattern = r'"signature",\s?([a-zA-Z0-9$]+)\('
    logger.debug('finding initial function name')
    return regex_search(pattern, js, group=1)


def get_transform_plan(js):
    """Extracts the "transform plan", that is, the functions the original
    signature is passed through to decode the actual signature.

    :param str js:
        The contents of the base.js asset file.

    Sample Output:
    ~~~~~~~~~~~~~~
    ['DE.AJ(a,15)',
     'DE.VR(a,3)',
     'DE.AJ(a,51)',
     'DE.VR(a,3)',
     'DE.kT(a,51)',
     'DE.kT(a,8)',
     'DE.VR(a,3)',
     'DE.kT(a,21)']

    """
    name = re.escape(get_initial_function_name(js))
    pattern = r'%s=function\(\w\){[a-z=\.\(\"\)]*;(.*);(?:.+)}' % name
    logger.debug('getting transform plan')
    return regex_search(pattern, js, group=1).split(';')


def get_transform_object(js, var):
    """Extracts the "transform object" which contains the function definitions
    referenced in the "transform plan". The ``var`` argument is the obfuscated
    variable name which contains these functions, for example, given the
    function call ``DE.AJ(a,15)`` returned by the transform plan, "DE" would be
    the var.

    :param str js:
        The contents of the base.js asset file.
    :param str var:
        The obfuscated variable name that stores an object with all functions
        that descrambles the signature.

    Sample Output:
    ~~~~~~~~~~~~~~
    ['AJ:function(a){a.reverse()}',
     'VR:function(a,b){a.splice(0,b)}',
     'kT:function(a,b){var c=a[0];a[0]=a[b%a.length];a[b]=c}']

    """
    pattern = r'var %s={(.*?)};' % re.escape(var)
    logger.debug('getting transform object')
    return (
        regex_search(pattern, js, group=1, flags=re.DOTALL)
        .replace('\n', ' ')
        .split(', ')
    )


@memoize
def get_transform_map(js, var):
    """Builds a lookup table of obfuscated JavaScript function names to the
    Python equivalents.

    :param str js:
        The contents of the base.js asset file.
    :param str var:
        The obfuscated variable name that stores an object with all functions
        that descrambles the signature.

    """
    transform_object = get_transform_object(js, var)
    mapper = {}
    for obj in transform_object:
        # AJ:function(a){a.reverse()} => AJ, function(a){a.reverse()}
        name, function = obj.split(':', 1)
        fn = map_functions(function)
        mapper[name] = fn
    return mapper


def reverse(arr, b):
    """Immutable equivalent to function(a, b) { a.reverse() }. This method
    takes an unused ``b`` variable as their transform functions universally
    sent two arguments.

    Example usage:
    ~~~~~~~~~~~~~~
    >>> reverse([1, 2, 3, 4])
    [4, 3, 2, 1]
    """
    return arr[::-1]


def splice(arr, b):
    """Immutable equivalent to function(a, b) { a.splice(0, b) }.

    Example usage:
    ~~~~~~~~~~~~~~
    >>> splice([1, 2, 3, 4], 2)
    [1, 2]
    """
    return arr[:b] + arr[b * 2:]


def swap(arr, b):
    """Immutable equivalent to:
    function(a, b) { var c=a[0];a[0]=a[b%a.length];a[b]=c }.

    Example usage:
    ~~~~~~~~~~~~~~
    >>> swap([1, 2, 3, 4], 2)
    [3, 2, 1, 4]
    """
    r = b % len(arr)
    return list(chain([arr[r]], arr[1:r], [arr[0]], arr[r + 1:]))


def map_functions(js_func):
    """For a given JavaScript transform function, return the Python equivalent.

    :param str js_func:
        The JavaScript version of the transform function.

    """
    mapper = (
        # function(a){a.reverse()}
        ('{\w\.reverse\(\)}', reverse),
        # function(a,b){a.splice(0,b)}
        ('{\w\.splice\(0,\w\)}', splice),
        # function(a,b){var c=a[0];a[0]=a[b%a.length];a[b]=c}
        ('{var\s\w=\w\[0\];\w\[0\]=\w\[\w\%\w.length\];\w\[\w\]=\w}', swap),
    )

    for pattern, fn in mapper:
        if re.search(pattern, js_func):
            return fn
    raise RegexMatchError(
        'could not find python equivalent function for: ',
        js_func,
    )


def parse_function(js_func):
    """Breaks a JavaScript transform function down into a two element tuple
    containing the function name and some integer-based argument.

    Sample Input:
    ~~~~~~~~~~~~~
    DE.AJ(a,15)

    Sample Output:
    ~~~~~~~~~~~~~~
    ('AJ', 15)

    :param str js_func:
        The JavaScript version of the transform function.

    """
    pattern = r'\w+\.(\w+)\(\w,(\d+)\)'
    logger.debug('parsing transform function')
    return regex_search(pattern, js_func, groups=True)


@memoize
def get_signature(js, ciphered_signature):
    """Taking the ciphered signature, applies the transform functions and
    returns the decrypted version.

    :param str js:
        The contents of the base.js asset file.
    :param str ciphered_signature:
        The ciphered signature sent in the ``player_config``.

    """
    tplan = get_transform_plan(js)
    # DE.AJ(a,15) => DE, AJ(a,15)
    var, _ = tplan[0].split('.')
    tmap = get_transform_map(js, var)
    signature = [s for s in ciphered_signature]

    for js_func in tplan:
        name, argument = parse_function(js_func)
        signature = tmap[name](signature, int(argument))
        logger.debug(
            'applied transform function\n%s', pprint.pformat(
                {
                    'output': ''.join(signature),
                    'js_function': name,
                    'argument': int(argument),
                    'function': tmap[name],
                }, indent=2,
            ),
        )
    return ''.join(signature)
