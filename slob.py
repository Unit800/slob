#pylint: disable=C0111,C0103,C0302,R0903,R0904,R0914,R0201
import encodings
import io
import os
import sys
import tempfile
import unicodedata
import unittest
import functools

import warnings
import pickle
import random

from builtins import open as fopen
from uuid import uuid4, UUID
from collections.abc import Sequence
from bisect import bisect_left
from collections import namedtuple
from functools import lru_cache
from struct import pack, unpack, calcsize
from types import MappingProxyType
from threading import RLock
from abc import abstractmethod

import icu

DEFAULT_COMPRESSION = 'lzma2'

UTF8 = 'utf-8'
MAGIC = b'!-1SLOB\x1F'

Compression = namedtuple('Compression', 'compress decompress')

Ref = namedtuple('Ref', 'key bin_index item_index fragment')

Header = namedtuple('Header',
                    'magic uuid encoding '
                    'compression tags content_types '
                    'blob_count '
                    'store_offset '
                    'refs_offset '
                    'size')

U_CHAR = '>B'
U_CHAR_SIZE = calcsize(U_CHAR)
U_SHORT = '>H'
U_SHORT_SIZE = calcsize(U_SHORT)
U_INT = '>I'
U_INT_SIZE = calcsize(U_INT)
U_LONG_LONG = '>Q'
U_LONG_LONG_SIZE = calcsize(U_LONG_LONG)

MAX_TEXT_LEN = 2**(U_SHORT_SIZE*8) - 1
MAX_TINY_TEXT_LEN = 2**(U_CHAR_SIZE*8) - 1
MAX_LARGE_BYTE_STRING_LEN = 2**(U_INT_SIZE*8) - 1

from icu import Locale, Collator, UCollAttribute, UCollAttributeValue

PRIMARY = Collator.PRIMARY
SECONDARY = Collator.SECONDARY
TERTIARY = Collator.TERTIARY
QUATERNARY = Collator.QUATERNARY
IDENTICAL = Collator.IDENTICAL


def init_compressions():
    ident = lambda x: x
    compressions = {'': Compression(ident, ident)}
    for name in ('bz2', 'zlib'):
        try:
            m = __import__(name)
        except ImportError:
            warnings.warn('%s is not available' % name)
        else:
            compressions[name] = Compression(
                lambda x: m.compress(x, 9), m.decompress)

    try:
        import lzma
    except ImportError:
        warnings.warn('lzma is not available')
    else:
        filters = [{'id': lzma.FILTER_LZMA2}]
        compress = lambda s: lzma.compress(s,
                                           format=lzma.FORMAT_RAW,
                                           filters=filters)
        decompress = lambda s: lzma.decompress(s,
                                               format=lzma.FORMAT_RAW,
                                               filters=filters)
        compressions['lzma2'] = Compression(compress, decompress)
    return compressions

COMPRESSIONS = init_compressions()


del init_compressions


class FileFormatException(Exception):
    pass


class UnknownFileFormat(FileFormatException):
    pass


class UnknownCompression(FileFormatException):
    pass


class UnknownEncoding(FileFormatException):
    pass


class IncorrectFileSize(FileFormatException):
    pass


@lru_cache(maxsize=None)
def sortkey(strength, maxlength=None):
    c = Collator.createInstance(Locale(''))
    c.setStrength(strength)
    c.setAttribute(UCollAttribute.ALTERNATE_HANDLING,
                   UCollAttributeValue.SHIFTED)
    if maxlength is None:
        return c.getSortKey
    else:
        return lambda x: c.getSortKey(x)[:maxlength]


def sortkey_length(strength, word):
    c = Collator.createInstance(Locale(''))
    c.setStrength(strength)
    c.setAttribute(UCollAttribute.ALTERNATE_HANDLING,
                   UCollAttributeValue.SHIFTED)
    coll_key = c.getSortKey(word)
    return len(coll_key) - 1 #subtract 1 for ending \x00 byte


class MultiFileReader(io.BufferedIOBase):

    def __init__(self, *args):
        filenames = []
        for arg in args:
            if isinstance(arg, str):
                filenames.append(arg)
            else:
                for name in arg:
                    filenames.append(name)
        files = []
        ranges = []
        offset = 0
        for name in filenames:
            size = os.stat(name).st_size
            ranges.append(range(offset, offset+size))
            files.append(fopen(name, 'rb'))
            offset += size
        self.size = offset
        self._ranges = ranges
        self._files = files
        self._fcount = len(self._files)
        self._offset = -1
        self.seek(0)

    def  __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    def close(self):
        for f in self._files:
            f.close()
        self._files.clear()
        self._ranges.clear()

    def closed(self):
        return len(self._ranges) == 0

    def isatty(self):
        return False

    def readable(self):
        return True

    def seek(self, offset, whence=io.SEEK_SET):
        if whence == io.SEEK_SET:
            self._offset = offset
        elif whence == io.SEEK_CUR:
            self._offset = self._offset + offset
        elif whence == io.SEEK_END:
            self._offset = self.size + offset
        else:
            raise ValueError('Invalid value for parameter whence: %r' % whence)
        return self._offset

    def seekable(self):
        return True

    def tell(self):
        return self._offset

    def writable(self):
        return False

    def read(self, n=-1):
        file_index = -1
        actual_offset = 0
        for i, r in enumerate(self._ranges):
            if self._offset in r:
                file_index = i
                actual_offset = self._offset - r.start
                break
        result = b''
        if (n == -1 or n is None):
            to_read = self.size
        else:
            to_read = n
        while -1 < file_index < self._fcount:
            f = self._files[file_index]
            f.seek(actual_offset)
            read = f.read(to_read)
            read_count = len(read)
            self._offset += read_count
            result += read
            to_read -= read_count
            if to_read > 0:
                file_index += 1
                actual_offset = 0
            else:
                break
        return result


class CollationKeyList(object):

    def __init__(self, lst, sortkey_):
        self.lst = lst
        self.sortkey = sortkey_

    def __len__(self):
        return len(self.lst)

    def __getitem__(self, i):
        return self.sortkey(self.lst[i].key)


class KeydItemDict(object):

    def __init__(self, lst, strength, maxlength=None):
        self.lst = lst
        self.sortkey = sortkey(strength, maxlength=maxlength)
        self.sortkeylist = CollationKeyList(lst, self.sortkey)

    def __len__(self):
        return len(self.lst)

    def __getitem__(self, key):
        key_as_sk = self.sortkey(key)
        i = bisect_left(self.sortkeylist, key_as_sk)
        if i != len(self.lst):
            while i < len(self.lst):
                if (self.sortkey(self.lst[i].key) == key_as_sk):
                    yield self.lst[i]
                else:
                    break
                i += 1

    def __contains__(self, key):
        try:
            next(self[key])
        except StopIteration:
            return False
        else:
            return True


class Blob(object):

    def __init__(self, content_id, key, fragment, read_func):
        self._content_id = content_id
        self._key = key
        self._fragment = fragment
        self._read = read_func

    @property
    def id(self):
        return self._content_id

    @property
    def key(self):
        return self._key

    @property
    def fragment(self):
        return self._fragment

    @property
    def content_type(self):
        return self._read()[0]

    @property
    def content(self):
        return self._read()[1]

    def __str__(self):
        return self.key

    def __repr__(self):
        return ('<{0.__class__.__module__}.{0.__class__.__name__} '
                '{0.key}>'.format(self))


def read_byte_string(f, len_spec):
    length = unpack(len_spec, f.read(calcsize(len_spec)))[0]
    return f.read(length)


class StructReader:

    def __init__(self, file_, encoding=None):
        self._file = file_
        self.encoding = encoding

    def read_int(self):
        s = self.read(U_INT_SIZE)
        return unpack(U_INT, s)[0]

    def read_long(self):
        b = self.read(U_LONG_LONG_SIZE)
        return unpack(U_LONG_LONG, b)[0]

    def read_byte(self):
        s = self.read(U_CHAR_SIZE)
        return unpack(U_CHAR, s)[0]

    def read_short(self):
        return unpack(U_SHORT, self._file.read(U_SHORT_SIZE))[0]

    def _read_text(self, len_spec):
        return read_byte_string(self._file, len_spec).decode(self.encoding)

    def read_tiny_text(self):
        return self._read_text(U_CHAR)

    def read_text(self):
        return self._read_text(U_SHORT)

    def __getattr__(self, name):
        return getattr(self._file, name)


class StructWriter:

    def __init__(self, file_, encoding=None):
        self._file = file_
        self.encoding = encoding

    def write_int(self, value):
        self._file.write(pack(U_INT, value))

    def write_long(self, value):
        self._file.write(pack(U_LONG_LONG, value))

    def write_byte(self, value):
        self._file.write(pack(U_CHAR, value))

    def write_short(self, value):
        self._file.write(pack(U_SHORT, value))

    def _write_text(self, text, len_size_spec, encoding=None):
        if encoding is None:
            encoding = self.encoding
        text_bytes = text.encode(encoding)
        length = len(text_bytes)
        self._file.write(pack(len_size_spec, length))
        self._file.write(text_bytes)

    def write_tiny_text(self, text, encoding=None):
        self._write_text(text, U_CHAR, encoding=encoding)

    def write_text(self, text, encoding=None):
        self._write_text(text, U_SHORT, encoding=encoding)

    def __getattr__(self, name):
        return getattr(self._file, name)


def read_header(f):
    f.seek(0)

    magic = f.read(len(MAGIC))
    if (magic != MAGIC):
        raise UnknownFileFormat()

    uuid = UUID(bytes=f.read(16))
    encoding = read_byte_string(f, U_CHAR).decode(UTF8)
    if encodings.search_function(encoding) is None:
        raise UnknownEncoding(encoding)

    f = StructReader(f, encoding)
    compression = f.read_tiny_text()
    if not compression in COMPRESSIONS:
        raise UnknownCompression(compression)

    def read_tags():
        tags = {}
        count = f.read_byte()
        for _ in range(count):
            key = f.read_tiny_text()
            value = f.read_tiny_text()
            tags[key] = value
        return tags
    tags = read_tags()

    def read_content_types():
        content_types = []
        count = f.read_byte()
        for _ in range(count):
            content_type = f.read_text()
            content_types.append(content_type)
        return tuple(content_types)
    content_types = read_content_types()

    blob_count = f.read_int()
    store_offset = f.read_long()
    size = f.read_long()
    refs_offset = f.tell()

    return Header(magic=magic,
                  uuid=uuid,
                  encoding=encoding,
                  compression=compression,
                  tags=MappingProxyType(tags),
                  content_types=content_types,
                  blob_count=blob_count,
                  store_offset=store_offset,
                  refs_offset=refs_offset,
                  size=size)


def meld_ints(a, b):
    return (a << 16) | b


def unmeld_ints(c):
    bstr = bin(c).lstrip("0b").zfill(48)
    a, b = bstr[-48:-16], bstr[-16:]
    return int(a, 2), int(b, 2)


class Slob(Sequence):

    def __init__(self, file_or_filenames):
        self._f = MultiFileReader(file_or_filenames)

        try:
            self._header = read_header(self._f)
            if (self._f.size != self._header.size):
                raise IncorrectFileSize(
                    'File size should be {0}, {1} bytes found'
                    .format(self._header.size, self._f.size))
        except FileFormatException:
            self._f.close()
            raise

        self._refs = RefList(self._f,
                             self._header.encoding,
                             offset=self._header.refs_offset)

        self._g = MultiFileReader(file_or_filenames)
        self._store = Store(self._g,
                            self._header.store_offset,
                            COMPRESSIONS[self._header.compression].decompress,
                            self._header.content_types)

    def  __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False

    @property
    def id(self):
        return self._header.uuid.hex

    @property
    def tags(self):
        return self._header.tags

    @property
    def blob_count(self):
        return self._header.blob_count

    def __len__(self):
        return len(self._refs)

    def __getitem__(self, i):
        ref = self._refs[i]
        read_func = lru_cache(maxsize=None)(functools.partial(self._store.get,
                                                              ref.bin_index,
                                                              ref.item_index))
        content_id = meld_ints(ref.bin_index, ref.item_index)
        return Blob(content_id, ref.key, ref.fragment, read_func)

    def get(self, blob_id):
        bin_index, bin_item_index = unmeld_ints(blob_id)
        return self._store.get(bin_index, bin_item_index)

    @lru_cache(maxsize=None)
    def as_dict(self,
                strength=TERTIARY,
                maxlength=None):
        return KeydItemDict(self, strength, maxlength=maxlength)

    def close(self):
        self._f.close()
        self._g.close()


def find_parts(fname):
    fname = os.path.expanduser(fname)
    dirname = os.path.dirname(fname) or os.getcwd()
    basename = os.path.basename(fname)
    candidates = []
    for name in os.listdir(dirname):
        if name.startswith(basename):
            candidates.append(os.path.join(dirname, name))
    return sorted(candidates)


def open(file_or_filenames):
    if isinstance(file_or_filenames, str):
        if not os.path.exists(file_or_filenames):
            file_or_filenames = find_parts(file_or_filenames)
    return Slob(file_or_filenames)


def create(*args, **kwargs):
    return Writer(*args, **kwargs)


class BinMemWriter:

    def __init__(self):
        self.item_dir = []
        self.items = []
        self.current_offset = 0

    def add(self, content_type_id, blob):
        self.item_dir.append(pack(U_INT, self.current_offset))
        type_length_and_bytes = (pack(U_CHAR, content_type_id) +
                                 pack(U_INT, len(blob)) + blob)
        self.items.append(type_length_and_bytes)
        self.current_offset += len(type_length_and_bytes)

    def __len__(self):
        return len(self.item_dir)

    def finalize(self, fout: 'output file', compress: 'function'):
        content = b''.join([pack(U_INT, len(self))]+self.item_dir + self.items)
        compressed = compress(content)
        fout.write(pack(U_INT, len(compressed)))
        fout.write(compressed)
        self.item_dir.clear()
        self.items.clear()


class ItemList(Sequence):

    def __init__(self, file_, offset,
                 count_or_spec, pos_spec,
                 cache_size=None):
        self.lock = RLock()
        self._file = file_
        file_.seek(offset)
        if isinstance(count_or_spec, str):
            count_spec = count_or_spec
            self.count = unpack(count_spec, file_.read(calcsize(count_spec)))[0]
        else:
            self.count = count_or_spec
        self.pos_offset = file_.tell()
        self.pos_spec = pos_spec
        self.pos_size = calcsize(pos_spec)
        self.data_offset = self.pos_offset + self.pos_size * self.count
        if cache_size:
            self.__getitem__ = lru_cache(maxsize=cache_size)(self.__getitem__)

    def __len__(self):
        return self.count

    def pos(self, i):
        with self.lock:
            self._file.seek(self.pos_offset + self.pos_size*i)
            return unpack(self.pos_spec, self._file.read(self.pos_size))[0]

    def read(self, pos):
        with self.lock:
            self._file.seek(self.data_offset + pos)
            return self._read_item()

    @abstractmethod
    def _read_item(self):
        pass

    def __getitem__(self, i):
        if i >= len(self) or i < 0:
            raise IndexError('index out of range')
        return self.read(self.pos(i))


class RefList(ItemList):

    def __init__(self, f, encoding, offset=0, count=None):
        super().__init__(StructReader(f, encoding),
                         offset,
                         U_INT if count is None else count,
                         U_LONG_LONG,
                         cache_size=512)


    def _read_item(self):
        key = self._file.read_text()
        bin_index = self._file.read_int()
        item_index = self._file.read_short()
        fragment = self._file.read_tiny_text()
        return Ref(key=key,
                   bin_index=bin_index,
                   item_index=item_index,
                   fragment=fragment)

    @lru_cache(maxsize=None)
    def as_dict(self,
                strength=TERTIARY,
                maxlength=None):
        return KeydItemDict(self, strength, maxlength=maxlength)


class Bin(ItemList):

    def __init__(self, bin_bytes):
        super().__init__(StructReader(io.BytesIO(bin_bytes)),
                         0,
                         U_INT,
                         U_INT)

    def _read_item(self):
        content_type_id = self._file.read_byte()
        content_len = self._file.read_int()
        content = self._file.read(content_len)
        return content_type_id, content


class Store(ItemList):

    def __init__(self, file_, offset, decompress, content_types):
        super().__init__(StructReader(file_),
                         offset,
                         U_INT,
                         U_LONG_LONG,
                         cache_size=32)
        self.decompress = decompress
        self.content_types = content_types

    def _read_item(self):
        compressed_length = self._file.read_int()
        compressed = self._file.read(compressed_length)
        bin_bytes = self.decompress(compressed)
        return Bin(bin_bytes)

    def get(self, bin_index, item_index):
        content_type_id, content = self[bin_index][item_index]
        content_type = self.content_types[content_type_id]
        return (content_type, content)


def find(word, slobs, match_prefix=True):
    seen = set()
    if isinstance(slobs, Slob):
        slobs = [slobs]

    maxlengths = (None, len(word)) if match_prefix else (None,)

    variants = []

    for strength in (IDENTICAL, QUATERNARY, TERTIARY, SECONDARY, PRIMARY):
        variants.append((strength, None))

    for strength in (IDENTICAL, QUATERNARY, TERTIARY, SECONDARY, PRIMARY):
        variants.append((strength, sortkey_length(strength, word)))

    for strength, maxlength in variants:
        for slob in slobs:
            d = slob.as_dict(strength=strength, maxlength=maxlength)
            for item in d[word]:
                dedup_key = (slob.id, item.key, item.id)
                if dedup_key in seen:
                    continue
                else:
                    seen.add(dedup_key)
                    yield slob, item


WriterEvent = namedtuple('WriterEvent', 'name data')


class KeyTooLongException(Exception):

    @property
    def key(self):
        return self.args[0]


class Writer(object):

    def __init__(self,
                 filename,
                 workdir=None,
                 encoding=UTF8,
                 compression=DEFAULT_COMPRESSION,
                 min_bin_size=512*1024,
                 max_redirects=5,
                 observer=None):
        self.filename = filename
        self.observer = observer
        if os.path.exists(self.filename):
            raise SystemExit('File %r already exists' % self.filename)

        #make sure we can write
        with fopen(self.filename, 'wb'):
            pass

        self.encoding = encoding

        if encodings.search_function(self.encoding) is None:
            raise UnknownEncoding(self.encoding)

        self.workdir = workdir

        self.tmpdir = tmpdir = tempfile.TemporaryDirectory(
            prefix='{0}-'.format(os.path.basename(filename)),
            dir=workdir)

        self.f_ref_positions = self._wbfopen('ref-positions')
        self.f_store_positions = self._wbfopen('store-positions')
        self.f_refs = self._wbfopen('refs')
        self.f_store = self._wbfopen('store')

        self.max_redirects = max_redirects
        if max_redirects:
            self.aliases_path = os.path.join(tmpdir.name, 'aliases')
            self.f_aliases = Writer(self.aliases_path,
                                    workdir=tmpdir.name,
                                    max_redirects=0,
                                    compression=None)

        if compression is None:
            compression = ''
        if not compression in COMPRESSIONS:
            raise UnknownCompression(compression)
        else:
            self.compress = COMPRESSIONS[compression].compress

        self.compression = compression
        self.content_types = {}

        self.min_bin_size = min_bin_size

        self.current_bin = None

        self.blob_count = 0
        self.ref_count = 0
        self.bin_count = 0
        self._tags = {
            'version.python': sys.version,
            'version.pyicu': icu.VERSION,
            'version.icu': icu.ICU_VERSION
        }
        self.tags = MappingProxyType(self._tags)

    def _wbfopen(self, name):
        return StructWriter(
            fopen(os.path.join(self.tmpdir.name, name), 'wb'),
            encoding=self.encoding)

    def tag(self, name, value=''):
        if len(name) > MAX_TINY_TEXT_LEN or len(value) > MAX_TINY_TEXT_LEN:
            self._fire_event('tag_too_long', (name, value))
            return
        self._tags[name] = value

    def _split_key(self, key):
        if isinstance(key, str):
            actual_key = key
            fragment = ''
        else:
            actual_key, fragment = key
        if (len(actual_key) > MAX_TEXT_LEN or
            len(fragment) > MAX_TINY_TEXT_LEN):
            raise KeyTooLongException(key)
        return actual_key, fragment

    def add(self, blob, *keys, content_type=''):

        if len(blob) > MAX_LARGE_BYTE_STRING_LEN:
            self._fire_event('content_too_long', blob)
            return

        if len(content_type) > MAX_TEXT_LEN:
            self._fire_event('content_type_too_long', content_type)
            return

        actual_keys = []

        for key in keys:
            try:
                actual_key, fragment = self._split_key(key)
            except KeyTooLongException as e:
                self._fire_event('key_too_long', e.key)
            else:
                actual_keys.append((actual_key, fragment))

        if len(actual_keys) == 0:
            return

        if self.current_bin is None:
            self.current_bin = BinMemWriter()
            self.bin_count += 1

        if content_type not in self.content_types:
            self.content_types[content_type] = len(self.content_types)

        self.current_bin.add(self.content_types[content_type], blob)
        self.blob_count += 1
        bin_item_index = len(self.current_bin) - 1
        bin_index = self.bin_count - 1

        for actual_key, fragment in actual_keys:
            self._write_ref(actual_key, bin_index, bin_item_index, fragment)

        if self.current_bin.current_offset > self.min_bin_size:
            self._write_current_bin()

    def add_alias(self, key, target_key):
        if self.max_redirects:
            try:
                self._split_key(key)
            except KeyTooLongException as e:
                self._fire_event('alias_too_long', e.key)
                return
            try:
                self._split_key(target_key)
            except KeyTooLongException as e:
                self._fire_event('alias_target_too_long', e.key)
                return
            self.f_aliases.add(pickle.dumps(target_key), key)
        else:
            raise NotImplementedError()

    def _fire_event(self, name, data=None):
        if self.observer:
            self.observer(WriterEvent(name, data))

    def _write_current_bin(self):
        self.f_store_positions.write_long(self.f_store.tell())
        self.current_bin.finalize(self.f_store, self.compress)
        self.current_bin = None

    def _write_ref(self, key, bin_index, item_index, fragment=''):
        self.f_ref_positions.write_long(self.f_refs.tell())
        self.f_refs.write_text(key)
        self.f_refs.write_int(bin_index)
        self.f_refs.write_short(item_index)
        self.f_refs.write_tiny_text(fragment)
        self.ref_count += 1

    def _sort(self):
        self._fire_event('begin_sort')
        f_ref_positions_sorted = self._wbfopen('ref-positions-sorted')
        self.f_refs.flush()
        self.f_ref_positions.close()
        with MultiFileReader(self.f_ref_positions.name, self.f_refs.name) as f:
            ref_list = RefList(f, self.encoding, count=self.ref_count)
            sortkey_func = sortkey(IDENTICAL)
            for i in sorted(range(len(ref_list)),
                            key=lambda j: sortkey_func(ref_list[j].key)):
                ref_pos = ref_list.pos(i)
                f_ref_positions_sorted.write_long(ref_pos)
        f_ref_positions_sorted.close()
        os.remove(self.f_ref_positions.name)
        os.rename(f_ref_positions_sorted.name, self.f_ref_positions.name)
        self.f_ref_positions = StructWriter(
            fopen(self.f_ref_positions.name, 'ab'),
            encoding=self.encoding)
        self._fire_event('end_sort')

    def _resolve_aliases(self):
        self._fire_event('begin_resolve_aliases')
        self.f_aliases.finalize()
        with MultiFileReader(self.f_ref_positions.name,
                             self.f_refs.name) as f_ref_list:
            ref_list = RefList(f_ref_list, self.encoding, count=self.ref_count)
            ref_dict = ref_list.as_dict()
            with open(self.aliases_path) as r:
                aliases = r.as_dict()
                path = os.path.join(self.tmpdir.name, 'resolved-aliases')
                with create(path,
                            workdir=self.tmpdir.name,
                            max_redirects=0,
                            compression=None) as alias_writer:

                    def read_key_frag(item, default_fragment):
                        key_frag = pickle.loads(item.content)
                        if isinstance(key_frag, str):
                            return key_frag, default_fragment
                        else:
                            return key_frag

                    for item in r:
                        from_key = item.key
                        keys = set()
                        keys.add(from_key)
                        to_key, fragment = read_key_frag(item, item.fragment)
                        count = 0
                        while count <= self.max_redirects:
                            #is target key itself a redirect?
                            try:
                                orig_to_key = to_key
                                to_key, fragment = read_key_frag(
                                    next(aliases[to_key]), fragment)
                                count += 1
                                keys.add(orig_to_key)
                            except StopIteration:
                                break
                        if count > self.max_redirects:
                            self._fire_event('too_many_redirects', from_key)
                        try:
                            target_ref = next(ref_dict[to_key])
                        except StopIteration:
                            self._fire_event('alias_target_not_found', to_key)
                        else:
                            for key in keys:
                                ref = Ref(key=key,
                                          bin_index=target_ref.bin_index,
                                          item_index=target_ref.item_index,
                                          #last fragment in the chain wins
                                          fragment=target_ref.fragment or fragment)
                                alias_writer.add(pickle.dumps(ref), key)

        with open(path) as resolved_aliases_reader:
            previous_key = None
            for item in resolved_aliases_reader:
                ref = pickle.loads(item.content)
                if ref.key == previous_key:
                    continue
                self._write_ref(ref.key, ref.bin_index,
                                ref.item_index, ref.fragment)
                previous_key = ref.key
        self._sort()
        self._fire_event('end_resolve_aliases')


    def finalize(self):
        self._fire_event('begin_finalize')
        if not self.current_bin is None:
            self._write_current_bin()

        self._sort()
        if self.max_redirects:
            self._resolve_aliases()

        files = (self.f_ref_positions,
                 self.f_refs,
                 self.f_store_positions,
                 self.f_store)

        for f in files:
            f.close()

        buf_size = 10*1024*1024

        with fopen(self.filename, mode='wb') as output_file:
            out = StructWriter(output_file, self.encoding)
            out.write(MAGIC)
            out.write(uuid4().bytes)
            out.write_tiny_text(self.encoding, encoding=UTF8)
            out.write_tiny_text(self.compression)

            def write_tags(tags, f):
                f.write(pack(U_CHAR, len(tags)))
                for key, value in tags.items():
                    f.write_tiny_text(key)
                    f.write_tiny_text(value)
            write_tags(self.tags, out)

            def write_content_types(content_types, f):
                count = len(content_types)
                f.write(pack(U_CHAR, count))
                types = sorted(content_types.items(), key=lambda x: x[1])
                for content_type, _ in types:
                    f.write_text(content_type)
            write_content_types(self.content_types, out)

            out.write_int(self.blob_count)
            store_offset = (out.tell() +
                                    U_LONG_LONG_SIZE + # this value
                                    U_LONG_LONG_SIZE + # file size value
                                    U_INT_SIZE + # ref count value
                                    os.stat(self.f_ref_positions.name).st_size +
                                    os.stat(self.f_refs.name).st_size)
            out.write_long(store_offset)
            out.flush()

            file_size = (out.tell() + # bytes written so far
                         U_LONG_LONG_SIZE + # file size value
                         2*U_INT_SIZE) # ref count and bin count
            file_size += sum((os.stat(f.name).st_size for f in files))
            out.write_long(file_size)

            def mv(src, out):
                fname = src.name
                self._fire_event('begin_move', fname)
                with fopen(fname, mode='rb') as f:
                    while True:
                        data = f.read(buf_size)
                        if len(data) == 0:
                            break
                        out.write(data)
                        out.flush()
                os.remove(fname)
                self._fire_event('end_move', fname)

            out.write_int(self.ref_count)
            mv(self.f_ref_positions, out)
            mv(self.f_refs, out)

            out.write_int(self.bin_count)
            mv(self.f_store_positions, out)
            mv(self.f_store, out)


        self.tmpdir.cleanup()
        self._fire_event('end_finalize')


    def  __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.finalize()
        return False


class TestReadWrite(unittest.TestCase):

    def setUp(self):

        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')
        self.path = os.path.join(self.tmpdir.name, 'test.slob')

        TEXT = 'text/plain'
        HTML = 'text/html'

        with create(self.path) as w:

            self.tags = {'a': 'abc',
                         'bb': 'xyz123',
                         'ccc': 'lkjlk'}
            for name, value in self.tags.items():
                w.tag(name, value)

            self.tag2 = 'bb', 'xyz123'

            self.blob_encoding = 'ascii'

            self.data = [
                (('c', 'cc', 'ccc'), TEXT, 'Hello C 1'),
                ('a', TEXT, 'Hello A 12'),
                ('z', TEXT, 'Hello Z 123'),
                ('b', TEXT, 'Hello B 1234'),
                ('d', TEXT, 'Hello D 12345'),
                ('uuu', HTML, '<html><body>Hello U!</body></html>'),
                ((('yy', 'frag1'),), HTML, '<h1 name="frag1">Section 1</h1>'),
            ]

            self.all_keys = []

            self.data_as_dict = {}

            for k, t, v in self.data:
                if isinstance(k, str):
                    k = (k,)
                for key in k:
                    if isinstance(key, tuple):
                        key, fragment = key
                    else:
                        fragment = ''
                    self.all_keys.append(key)
                    self.data_as_dict[key] = (t, v, fragment)
                w.add(v.encode(self.blob_encoding), *k, content_type=t)
            self.all_keys.sort()

        self.w = w

    def test_header(self):
        with MultiFileReader(self.path) as f:
            header = read_header(f)

        for key, value in self.tags.items():
            self.assertEqual(header.tags[key], value)

        self.assertEqual(self.w.encoding, UTF8)
        self.assertEqual(header.encoding, self.w.encoding)

        self.assertEqual(header.compression, self.w.compression)

        for i, content_type in enumerate(header.content_types):
            self.assertEqual(self.w.content_types[content_type], i)

        self.assertEqual(header.blob_count, len(self.data))

    def test_content(self):
        with open(self.path) as r:
            self.assertEqual(len(r), len(self.all_keys))
            self.assertRaises(IndexError,
                              r.__getitem__, len(self.all_keys))
            for i, item in enumerate(r):
                self.assertEqual(item.key, self.all_keys[i])
                content_type, value, fragment = self.data_as_dict[item.key]
                self.assertEqual(
                    item.content_type, content_type)
                self.assertEqual(
                    item.content.decode(self.blob_encoding), value)
                self.assertEqual(
                    item.fragment, fragment)


    def tearDown(self):
        self.tmpdir.cleanup()


class TestSort(unittest.TestCase):

    def setUp(self):

        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')
        self.path = os.path.join(self.tmpdir.name, 'test.slob')

        with create(self.path) as  w:

            data = [
                'Ф, ф',
                'Ф ф',
                'Ф',
                'Э',
                'Е е',
                'г',
                'н',
                'ф',
                'а',
                'Ф, Ф',
                'е',
                'Е',
                'Ее',
                'ё',
                'Ё',
                'Её',
                'Е ё',
                'А',
                'э',
                'ы'
            ]

            self.data_sorted = sorted(data, key=sortkey(IDENTICAL))

            for k in data:
                v = ';'.join(unicodedata.name(c) for c in k)
                w.add(v.encode('ascii'), k)

        self.r = open(self.path)

    def test_sort_order(self):
        for i in range(len(self.r)):
            self.assertEqual(self.r[i].key, self.data_sorted[i])

    def tearDown(self):
        self.r.close()
        self.tmpdir.cleanup()


class TestFind(unittest.TestCase):

    def setUp(self):

        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')
        self.path = os.path.join(self.tmpdir.name, 'test.slob')

        with create(self.path) as w:
            data = ['Cc', 'aA', 'aa', 'Aa', 'Bb', 'cc', 'Äā', 'ăÀ',
                    'a\u00A0a', 'a-a', 'a\u2019a', 'a\u2032a', 'a,a', 'a a']

            for k in data:
                v = ';'.join(unicodedata.name(c) for c in k)
                w.add(v.encode('ascii'), k)

        self.r = open(self.path)

    def get(self, d, key):
        return list(item.content.decode('ascii') for item in d[key])

    def test_find_identical(self):
        d = self.r.as_dict(IDENTICAL)
        self.assertEqual(
            self.get(d, 'aa'),
            ['LATIN SMALL LETTER A;LATIN SMALL LETTER A'])
        self.assertEqual(
            self.get(d, 'a-a'),
            ['LATIN SMALL LETTER A;HYPHEN-MINUS;LATIN SMALL LETTER A'])
        self.assertEqual(
            self.get(d, 'aA'),
            ['LATIN SMALL LETTER A;LATIN CAPITAL LETTER A'])
        self.assertEqual(
            self.get(d, 'Äā'),
            ['LATIN CAPITAL LETTER A WITH DIAERESIS;'
             'LATIN SMALL LETTER A WITH MACRON'])
        self.assertEqual(
            self.get(d, 'a a'),
            ['LATIN SMALL LETTER A;SPACE;LATIN SMALL LETTER A'])

    def test_find_quaternary(self):
        d = self.r.as_dict(QUATERNARY)
        self.assertEqual(
            self.get(d, 'a\u2032a'),
            ['LATIN SMALL LETTER A;PRIME;LATIN SMALL LETTER A'])
        self.assertEqual(
            self.get(d, 'a a'),
            ['LATIN SMALL LETTER A;SPACE;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;NO-BREAK SPACE;LATIN SMALL LETTER A'])

    def test_find_tertiary(self):
        d = self.r.as_dict(TERTIARY)
        self.assertEqual(
            self.get(d, 'aa'),
            ['LATIN SMALL LETTER A;SPACE;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;NO-BREAK SPACE;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;HYPHEN-MINUS;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;COMMA;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;RIGHT SINGLE QUOTATION MARK;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;PRIME;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;LATIN SMALL LETTER A'])

    def test_find_secondary(self):
        d = self.r.as_dict(SECONDARY)
        self.assertEqual(
            self.get(d, 'aa'),
            ['LATIN SMALL LETTER A;SPACE;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;NO-BREAK SPACE;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;HYPHEN-MINUS;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;COMMA;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;RIGHT SINGLE QUOTATION MARK;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;PRIME;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;LATIN CAPITAL LETTER A',
             'LATIN CAPITAL LETTER A;LATIN SMALL LETTER A'])


    def test_find_primary(self):
        d = self.r.as_dict(PRIMARY)

        self.assertEqual(
            self.get(d, 'aa'),
            ['LATIN SMALL LETTER A;SPACE;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;NO-BREAK SPACE;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;HYPHEN-MINUS;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;COMMA;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;RIGHT SINGLE QUOTATION MARK;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;PRIME;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A;LATIN CAPITAL LETTER A',
             'LATIN CAPITAL LETTER A;LATIN SMALL LETTER A',
             'LATIN SMALL LETTER A WITH BREVE;LATIN CAPITAL LETTER A WITH GRAVE',
             'LATIN CAPITAL LETTER A WITH DIAERESIS;LATIN SMALL LETTER A WITH MACRON'])

    def tearDown(self):
        self.r.close()
        self.tmpdir.cleanup()


class TestPrefixFind(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')
        self.path = os.path.join(self.tmpdir.name, 'test.slob')
        self.data = ['a', 'ab', 'abc', 'abcd', 'abcde']
        with create(self.path) as w:
            for k in self.data:
                w.add(k.encode('ascii'), k)

    def tearDown(self):
        self.tmpdir.cleanup()

    def test(self):
        with open(self.path) as r:
            for i, k in enumerate(self.data):
                d = r.as_dict(IDENTICAL, len(k))
                self.assertEqual(list(v.content.decode('ascii') for v in  d[k]),
                                 self.data[i:])


class TestBestMatch(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')
        self.path1 = os.path.join(self.tmpdir.name, 'test1.slob')
        self.path2 = os.path.join(self.tmpdir.name, 'test2.slob')

        data1 = ['aa', 'Aa', 'a-a', 'aabc', 'Äā', 'bb', 'aa']
        data2 = ['aa', 'aA', 'āā', 'a,a', 'a-a', 'aade', 'Äā', 'cc']

        with create(self.path1) as w:
            for key in data1:
                w.add(b'', key)

        with create(self.path2) as w:
            for key in data2:
                w.add(b'', key)

    def test_best_match(self):
        self.maxDiff = None
        with open(self.path1) as s1, \
             open(self.path2) as s2:
            result = find('aa', [s1, s2], match_prefix=True)
            actual = list((s.id, item.key) for s, item in result)
            expected = [(s1.id, 'aa'),
                        (s1.id, 'aa'),
                        (s2.id, 'aa'),
                        (s1.id, 'a-a'),
                        (s2.id, 'a-a'),
                        (s2.id, 'a,a'),
                        (s1.id, 'Aa'),
                        (s2.id, 'aA'),
                        (s1.id, 'Äā'),
                        (s2.id, 'Äā'),
                        (s2.id, 'āā'),
                        (s1.id, 'aabc'),
                        (s2.id, 'aade'),]
            self.assertEqual(expected, actual)

    def tearDown(self):
        self.tmpdir.cleanup()


class TestAlias(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')
        self.path = os.path.join(self.tmpdir.name, 'test.slob')

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_alias(self):
        too_many_redirects = []
        target_not_found = []

        def observer(event):
            if event.name == 'too_many_redirects':
                too_many_redirects.append(event.data)
            elif event.name == 'alias_target_not_found':
                target_not_found.append(event.data)

        with create(self.path, observer=observer) as w:
            data = ['z', 'b', 'q', 'a', 'u', 'g', 'p', 'n']
            for k in data:
                v = ';'.join(unicodedata.name(c) for c in k)
                w.add(v.encode('ascii'), k)

            w.add_alias('w', 'u')
            w.add_alias('y1', 'y2')
            w.add_alias('y2', 'y3')
            w.add_alias('y3', 'z')
            w.add_alias('ZZZ', 'YYY')

            w.add_alias('l3', 'l1')
            w.add_alias('l1', 'l2')
            w.add_alias('l2', 'l3')

            w.add_alias('a1', ('a', 'a-frag1'))
            w.add_alias('a2', 'a1')
            w.add_alias('a3', ('a2', 'a-frag2'))

            w.add_alias('g1', 'g')
            w.add_alias('g2', ('g1', 'g-frag1'))


        self.assertEqual(too_many_redirects, ['l1', 'l2', 'l3'])
        self.assertEqual(target_not_found, ['l2', 'l3', 'l1', 'YYY'])

        with open(self.path) as r:
            d = r.as_dict()
            def get(key):
                return list(item.content.decode('ascii') for item in d[key])
            self.assertEqual(get('w'), ['LATIN SMALL LETTER U'])
            self.assertEqual(get('y1'), ['LATIN SMALL LETTER Z'])
            self.assertEqual(get('y2'), ['LATIN SMALL LETTER Z'])
            self.assertEqual(get('y3'), ['LATIN SMALL LETTER Z'])
            self.assertEqual(get('ZZZ'), [])
            self.assertEqual(get('l1'), [])
            self.assertEqual(get('l2'), [])
            self.assertEqual(get('l3'), [])

            item_a1 = next(d['a1'])
            self.assertEqual(item_a1.content, b'LATIN SMALL LETTER A')
            self.assertEqual(item_a1.fragment, 'a-frag1')

            item_a2 = next(d['a2'])
            self.assertEqual(item_a2.content, b'LATIN SMALL LETTER A')
            self.assertEqual(item_a2.fragment, 'a-frag1')

            item_a3 = next(d['a3'])
            self.assertEqual(item_a3.content, b'LATIN SMALL LETTER A')
            self.assertEqual(item_a3.fragment, 'a-frag1')

            item_g1 = next(d['g1'])
            self.assertEqual(item_g1.content, b'LATIN SMALL LETTER G')
            self.assertEqual(item_g1.fragment, '')

            item_g2 = next(d['g2'])
            self.assertEqual(item_g2.content, b'LATIN SMALL LETTER G')
            self.assertEqual(item_g2.fragment, 'g-frag1')


class TestBlobId(unittest.TestCase):

    def test(self):
        max_i = 2**32 - 1
        max_j = 2**16 - 1
        i_values = [0, max_i] + [random.randint(1, max_i - 1)
                                 for _ in range(100)]
        j_values = [0, max_j] + [random.randint(1, max_j - 1)
                                 for _ in range(100)]
        for i in i_values:
            for j in j_values:
                self.assertEqual(unmeld_ints(meld_ints(i, j)), (i, j))


class TestMultiFileReader(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_read_all(self):
        fnames = []
        for name in 'abcdef':
            path = os.path.join(self.tmpdir.name, name)
            fnames.append(path)
            with fopen(path, 'wb') as f:
                f.write(name.encode(UTF8))
        with MultiFileReader(fnames) as m:
            self.assertEqual(m.read().decode(UTF8), 'abcdef')

    def test_seek_and_read(self):

        def mkfile(basename, content):
            part = os.path.join(self.tmpdir.name, basename)
            with fopen(part, 'wb') as f:
                f.write(content)
            return part

        content = b'abc\nd\nefgh\nij'
        part1 = mkfile('1', content[:4])
        part2 = mkfile('2', content[4:5])
        part3 = mkfile('3', content[5:])

        with MultiFileReader(part1, part2, part3) as m:
            self.assertEqual(m.size, len(content))
            m.seek(2)
            self.assertEqual(m.read(2), content[2:4])
            m.seek(1)
            self.assertEqual(m.read(len(content) - 2), content[1:-1])
            m.seek(-1, whence=io.SEEK_END)
            self.assertEqual(m.read(10), content[-1:])

            m.seek(4)
            m.seek(-2, whence=io.SEEK_CUR)
            self.assertEqual(m.read(3), content[2:5])


class TestFormatErrors(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_wrong_file_type(self):
        name = os.path.join(self.tmpdir.name, '1')
        with fopen(name, 'wb') as f:
            f.write(b'123')
        self.assertRaises(UnknownFileFormat, open, name)

    def test_truncated_file(self):
        name = os.path.join(self.tmpdir.name, '1')

        with create(name) as f:
            f.add(b'123', 'a')
            f.add(b'234', 'b',)

        with fopen(name, 'rb') as f:
            all_bytes = f.read()

        with fopen(name, 'wb') as f:
            f.write(all_bytes[:-1])

        self.assertRaises(IncorrectFileSize, open, name)

        with fopen(name, 'wb') as f:
            f.write(all_bytes)
            f.write(b'\n')

        self.assertRaises(IncorrectFileSize, open, name)


class TestFindParts(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_find_parts(self):
        names = [os.path.join(self.tmpdir.name, name)
                 for name in ('abc-1', 'abc-2', 'abc-3')]
        for name in names:
            with fopen(name, 'wb'):
                pass
        parts = find_parts(os.path.join(self.tmpdir.name, 'abc'))
        self.assertEqual(names, parts)


class TestTooLongText(unittest.TestCase):

    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix='test')
        self.path = os.path.join(self.tmpdir.name, 'test.slob')

    def tearDown(self):
        self.tmpdir.cleanup()

    def test_too_long(self):
        rejected_keys = []
        rejected_aliases = []
        rejected_alias_targets = []
        rejected_tags = []
        rejected_content_types = []
        def observer(event):
            if event.name == 'key_too_long':
                rejected_keys.append(event.data)
            elif event.name == 'alias_too_long':
                rejected_aliases.append(event.data)
            elif event.name == 'alias_target_too_long':
                rejected_alias_targets.append(event.data)
            elif event.name == 'tag_too_long':
                rejected_tags.append(event.data)
            elif event.name == 'content_type_too_long':
                rejected_content_types.append(event.data)

        long_tag_name = 't'*(MAX_TINY_TEXT_LEN+1)
        long_tag_value = 'v'*(MAX_TINY_TEXT_LEN+1)
        long_content_type = 'T'*(MAX_TEXT_LEN+1)
        long_key = 'c'*(MAX_TEXT_LEN+1)
        long_frag = 'd'*(MAX_TINY_TEXT_LEN+1)
        key_with_long_frag = ('d', long_frag)
        tag_with_long_name = (long_tag_name, 't3 value')
        tag_with_long_value = ('t1', long_tag_value)
        long_alias = 'f'*(MAX_TEXT_LEN+1)
        alias_with_long_frag = ('i', long_frag)
        long_alias_target = long_key
        long_alias_target_frag = key_with_long_frag

        with create(self.path, observer=observer) as w:

            w.tag(*tag_with_long_value)
            w.tag('t2', 't2 value')
            w.tag(*tag_with_long_name)

            data = ['a', 'b', long_key, key_with_long_frag]

            for k in data:
                if isinstance(k, str):
                    v = k.encode('ascii')
                else:
                    v = '#'.join(k).encode('ascii')
                w.add(v, k)

            w.add_alias('e', 'a')
            w.add_alias(long_alias, 'a')
            w.add_alias(alias_with_long_frag, 'a')
            w.add_alias('g', long_alias_target)
            w.add_alias('h', long_alias_target_frag)

            w.add(b'Hello', 'hello', content_type=long_content_type)

        self.assertEqual(rejected_keys,
                         [long_key, key_with_long_frag])
        self.assertEqual(rejected_aliases,
                         [long_alias, alias_with_long_frag])
        self.assertEqual(rejected_alias_targets,
                         [long_alias_target, long_alias_target_frag])
        self.assertEqual(rejected_tags,
                         [tag_with_long_value, tag_with_long_name])
        self.assertEqual(rejected_content_types,
                         [long_content_type])

        with open(self.path) as r:
            self.assertEqual(r.tags['t2'], 't2 value')
            self.assertFalse(tag_with_long_name[0] in r.tags)
            self.assertFalse(tag_with_long_value[0] in r.tags)
            d = r.as_dict()
            self.assertTrue('a' in d)
            self.assertTrue('b' in d)
            self.assertFalse(long_key in d)
            self.assertFalse(key_with_long_frag[0] in d)
            self.assertTrue('e' in d)
            self.assertFalse(long_alias in d)
            self.assertFalse('g' in d)


if __name__ == '__main__':
    unittest.main()
