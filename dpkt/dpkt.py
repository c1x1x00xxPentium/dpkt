# $Id: dpkt.py 43 2007-08-02 22:42:59Z jon.oberheide $
# -*- coding: utf-8 -*-
"""Simple packet creation and parsing."""
from __future__ import absolute_import, print_function

import copy
import struct
from functools import partial
from itertools import chain

from .compat import compat_ord, compat_izip, iteritems, ntole


class Error(Exception):
    pass


class UnpackError(Error):
    pass


class NeedData(UnpackError):
    pass


class PackError(Error):
    pass


# See the "creating parsers" documentation for how all of this works


class _MetaPacket(type):
    def __new__(cls, clsname, clsbases, clsdict):
        t = type.__new__(cls, clsname, clsbases, clsdict)

        # create getter and setter properties for the bit fields
        bit_fields = getattr(t, '__bit_fields__', {})
        for ph_name, field_defs in bit_fields.items():  # ph_name: name of the placeholder variable
            bits_total = sum(bf[1] for bf in field_defs)  # total size in bits
            bits_used = 0

            for (bf_name, bf_size) in field_defs:
                if not bf_name.startswith('_'):  # do not create properties for _private fields
                    shift = bits_total - bits_used - bf_size
                    mask = (2**bf_size - 1) << shift  # all zeroes except the field bits
                    mask_inv = (2**bits_total - 1) - mask  # inverse mask

                    def make_getter(ph_name=ph_name, mask=mask, shift=shift):
                        def getter_func(self):
                            ph_val = getattr(self, ph_name)
                            return (ph_val & mask) >> shift
                        return getter_func

                    def make_setter(ph_name=ph_name, mask_inv=mask_inv, shift=shift):
                        def setter_func(self, bf_val):
                            ph_val = getattr(self, ph_name)
                            val = (bf_val << shift) | (ph_val & mask_inv)
                            setattr(self, ph_name, val)
                        return setter_func

                    # delete property to set the bit field back to its default value
                    def make_delete(ph_name=ph_name, mask=mask, mask_inv=mask_inv):
                        def delete_func(self):
                            ph_val = getattr(self, ph_name)
                            ph_val_new = (self.__hdr_defaults__[ph_name] & mask) | (ph_val & mask_inv)
                            setattr(self, ph_name, ph_val_new)
                        return delete_func

                    clsdict[bf_name] = property(make_getter(), make_setter(), make_delete())

                bits_used += bf_size
                assert bits_total - bits_used >= 0
            assert bits_used == bits_total

            # make sure the sizes match
            for hdr in getattr(t, '__hdr__', []):
                if hdr[0] == ph_name:
                    assert bits_total == struct.calcsize(hdr[1]) * 8
                    break

        st = getattr(t, '__hdr__', None)
        if st is not None:
            # XXX - __slots__ only created in __new__()
            clsdict['__slots__'] = [x[0] for x in st] + ['data']
            t = type.__new__(cls, clsname, clsbases, clsdict)
            t.__hdr_fields__ = [x[0] for x in st]
            t.__hdr_fmt__ = getattr(t, '__byte_order__', '>') + ''.join([x[1] for x in st])
            t.__hdr_len__ = struct.calcsize(t.__hdr_fmt__)
            t.__hdr_defaults__ = dict(compat_izip(
                t.__hdr_fields__, [x[2] for x in st]))

        # optional map of functions for pretty printing
        # {field_name: callable(field_value) -> str, ..}
        # define as needed in the child protocol classes
        #t.__pprint_funcs__ = {}  - disabled here to keep the base class lightweight

        return t


class Packet(_MetaPacket("Temp", (object,), {})):
    r"""Base packet class, with metaclass magic to generate members from self.__hdr__.

    Attributes:
        __hdr__: Packet header should be defined as a list of
                 (name, structfmt, default) tuples.
        __byte_order__: Byte order, can be set to override the default ('>')

    Example:
    >>> class Foo(Packet):
    ...   __hdr__ = (('foo', 'I', 1), ('bar', 'H', 2), ('baz', '4s', 'quux'))
    ...
    >>> foo = Foo(bar=3)
    >>> foo
    Foo(bar=3)
    >>> str(foo)
    '\x00\x00\x00\x01\x00\x03quux'
    >>> foo.bar
    3
    >>> foo.baz
    'quux'
    >>> foo.foo = 7
    >>> foo.baz = 'whee'
    >>> foo
    Foo(baz='whee', foo=7, bar=3)
    >>> Foo('hello, world!')
    Foo(baz=' wor', foo=1751477356L, bar=28460, data='ld!')
    """

    def __init__(self, *args, **kwargs):
        """Packet constructor with ([buf], [field=val,...]) prototype.

        Arguments:

        buf -- optional packet buffer to unpack

        Optional keyword arguments correspond to members to set
        (matching fields in self.__hdr__, or 'data').
        """
        self.data = b''
        if args:
            try:
                self.unpack(args[0])
            except struct.error:
                if len(args[0]) < self.__hdr_len__:
                    raise NeedData('got %d, %d needed at least' % (len(args[0]), self.__hdr_len__))
                raise UnpackError('invalid %s: %r' %
                                  (self.__class__.__name__, args[0]))
        else:
            if hasattr(self, '__hdr_fields__'):
                for k in self.__hdr_fields__:
                    setattr(self, k, copy.copy(self.__hdr_defaults__[k]))

            for k, v in iteritems(kwargs):
                setattr(self, k, v)

        if hasattr(self, '__hdr_fmt__'):
            self._pack_hdr = partial(struct.pack, self.__hdr_fmt__)

        # construct __public_fields__ to be used inside __repr__ and pprint
        # the list can be customized in child classes to include or remove
        # fields to display

        l_ = []

        def add_property(prop_name):
            if isinstance(getattr(self.__class__, prop_name, None), property):
                l_.append(prop_name)
                # calc the default value for the property and add to __hdr_defaults__

        # maintain order of fields as defined in __hdr__
        for field_name, _, _ in getattr(self, '__hdr__', []):
            # public fields defined in __hdr__; "public" means not starting with an underscore
            if field_name[0] != '_':
                l_.append(field_name)  # (1)

            # if a field name starts with an underscore, and does NOT contain more underscores,
            # it is considered hidden and is ignored (good for fields reserved for future use)

            # if a field name starts with an underscore, and DOES contain more underscores,
            # it is viewed as a complex field where underscores separate the named properties
            # of the class;
            elif '_' in field_name[1:]:
                # (1) search for these properties in __bit_fields__ where they are explicitly defined
                if field_name in getattr(self, '__bit_fields__', {}):
                    for bf in self.__bit_fields__[field_name]:
                        add_property(bf[0])

                # (2) split by underscore into 1- and 2-component names and look for properties with such names;
                #   Examples:
                #    _foo -> ignore
                #    _foo_bar -> look for properties named "foo", "bar" and "foo_bar"
                else:
                    fns = field_name[1:].split('_')
                    for prop_name in chain(fns, ('_'.join(x) for x in zip(fns, fns[1:]))):
                        add_property(prop_name)

        # check for duplicates, there shouldn't be any
        assert len(l_) == len(set(l_))
        self.__public_fields__ = l_

    def __len__(self):
        return self.__hdr_len__ + len(self.data)

    # legacy
    def __iter__(self):
        return iter((fld, getattr(self, fld)) for fld in self.__class__.__hdr_fields__)

    def __getitem__(self, kls):
        """Return the 1st occurence of the underlying <kls> data layer, raise KeyError otherwise."""
        dd = self.data
        while isinstance(dd, Packet):
            if dd.__class__ == kls:
                return dd
            dd = dd.data
        raise KeyError(kls)

    def __contains__(self, kls):
        """Return True is the given <kls> data layer is present in the stack."""
        try:
            return bool(self.__getitem__(kls))
        except KeyError:
            return False

    def __repr__(self):
        l_ = []

        # 1. public fields defined in __hdr__, unless their value is default
        # 2. properties derived from _private fields defined in __hdr__ and __bit_fields__
        for field_name in self.__public_fields__:
            field_value = getattr(self, field_name)
            if ((field_name not in self.__hdr_defaults__) or
               (field_value != self.__hdr_defaults__[field_name])):
                l_.append('%s=%r' % (field_name, field_value))

        # 3. dynamically added fields from self.__dict__, unless they are _private
        l_.extend(
            ['%s=%r' % (attr_name, attr_value)
             for attr_name, attr_value in iteritems(self.__dict__)
             if attr_name[0] != '_' and                   # exclude _private attributes
                attr_name != self.data.__class__.__name__.lower()])  # exclude fields like ip.udp

        # 4. self.data when it's present
        if self.data:
            l_.append('data=%r' % self.data)
        return '%s(%s)' % (self.__class__.__name__, ', '.join(l_))

    def pprint(self, indent=1):
        """Human friendly pretty-print."""
        l_ = []

        def add_field(fn, fv):
            """name=value,  # pretty-print form (if available)"""
            try:
                l_.append('%s=%r,  # %s' % (fn, fv, self.__pprint_funcs__[fn](fv)))
            except (AttributeError, KeyError):
                l_.append('%s=%r,' % (fn, fv))

        for field_name in self.__public_fields__:
            add_field(field_name, getattr(self, field_name))

        for attr_name, attr_value in iteritems(self.__dict__):
            if (attr_name[0] != '_' and                   # exclude _private attributes
               attr_name != self.data.__class__.__name__.lower()):  # exclude fields like ip.udp
                if type(attr_value) == list and attr_value:  # expand non-empty lists to print one item per line
                    l_.append('%s=[' % attr_name)
                    for av1 in attr_value:
                        l_.append('  ' + repr(av1) + ',')  # XXX - TODO: support pretty-print
                    l_.append('],')
                else:
                    add_field(attr_name, attr_value)

        print('%s(' % self.__class__.__name__)  # class name, opening brace
        for ii in l_:
            print(' ' * indent, '%s' % ii)

        if self.data:
            if isinstance(self.data, Packet):  # recursively descend to lower layers
                print(' ' * indent, 'data=', end='')
                self.data.pprint(indent=indent + 2)
            else:
                print(' ' * indent, 'data=%r' % self.data)
        print(' ' * (indent - 1), end='')
        print(')  # %s' % self.__class__.__name__)  # closing brace  # class name

    def __str__(self):
        return str(self.__bytes__())

    def __bytes__(self):
        return self.pack_hdr() + bytes(self.data)

    def pack_hdr(self):
        """Return packed header string."""
        try:
            return self._pack_hdr(
                *[getattr(self, k) for k in self.__hdr_fields__]
            )
        except (TypeError, struct.error):
            vals = []
            for k in self.__hdr_fields__:
                v = getattr(self, k)
                if isinstance(v, tuple):
                    vals.extend(v)
                else:
                    vals.append(v)
            try:
                return struct.pack(self.__hdr_fmt__, *vals)
            except struct.error as e:
                raise PackError(str(e))

    def pack(self):
        """Return packed header + self.data string."""
        return bytes(self)

    def unpack(self, buf):
        """Unpack packet header fields from buf, and set self.data."""
        for k, v in compat_izip(self.__hdr_fields__,
                                struct.unpack(self.__hdr_fmt__, buf[:self.__hdr_len__])):
            setattr(self, k, v)
        self.data = buf[self.__hdr_len__:]


# XXX - ''.join([(len(`chr(x)`)==3) and chr(x) or '.' for x in range(256)])
__vis_filter = (
    b'................................ !"#$%&\'()*+,-./0123456789:;<=>?'
    b'@ABCDEFGHIJKLMNOPQRSTUVWXYZ[.]^_`abcdefghijklmnopqrstuvwxyz{|}~.'
    b'................................................................'
    b'................................................................')


def hexdump(buf, length=16):
    """Return a hexdump output string of the given buffer."""
    n = 0
    res = []
    while buf:
        line, buf = buf[:length], buf[length:]
        hexa = ' '.join(['%02x' % compat_ord(x) for x in line])
        line = line.translate(__vis_filter).decode('utf-8')
        res.append('  %04d:  %-*s %s' % (n, length * 3, hexa, line))
        n += length
    return '\n'.join(res)


def in_cksum_add(s, buf):
    n = len(buf)
    cnt = (n // 2) * 2
    a = struct.unpack('<{}H'.format(n // 2), buf[:cnt])  # unpack as little endian words
    res = s + sum(a)
    if cnt != n:
        res += compat_ord(buf[-1])
    return res


def in_cksum_done(s):
    s = (s >> 16) + (s & 0xffff)
    s += (s >> 16)
    return ntole(~s & 0xffff)


def in_cksum(buf):
    """Return computed Internet checksum."""
    return in_cksum_done(in_cksum_add(0, buf))


def test_utils():
    __buf = b'\x00\x01\x02\x03\x04\x05\x06\x07\x08\t\n\x0b\x0c\r\x0e'
    __hd = '  0000:  00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e     ...............'
    h = hexdump(__buf)
    assert (h == __hd)
    assert in_cksum_add(0, __buf) == 12600  # endianness
    c = in_cksum(__buf)
    assert (c == 51150)


# test Packet.__getitem__ and __contains__ methods
def test_getitem_contains():
    import pytest

    class Foo(Packet):
        __hdr__ = (('foo', 'I', 0),)

    class Bar(Packet):
        __hdr__ = (('bar', 'I', 0),)

    class Baz(Packet):
        __hdr__ = (('baz', 'I', 0),)

    class Zeb(Packet):
        pass

    ff = Foo(foo=1, data=Bar(bar=2, data=Baz(attr=Zeb())))

    # __contains__
    assert Bar in ff
    assert Baz in ff
    assert Baz in ff.data
    assert Zeb not in ff
    assert Zeb not in Baz()

    # __getitem__
    assert isinstance(ff[Bar], Bar)
    assert isinstance(ff[Baz], Baz)

    assert isinstance(ff[Bar][Baz], Baz)
    with pytest.raises(KeyError):
        ff[Baz][Bar]

    with pytest.raises(KeyError):
        ff[Zeb]

    with pytest.raises(KeyError):
        Bar()[Baz]


def test_pack_hdr_overflow():
    """Try to fit too much data into struct packing"""
    import pytest

    class Foo(Packet):
        __hdr__ = (
            ('foo', 'I', 1),
            ('bar', 'I', (1, 2)),
        )

    foo = Foo(foo=2**32)
    with pytest.raises(PackError):
        bytes(foo)


def test_pack_hdr_tuple():
    """Test the unpacking of a tuple for a single format string"""
    class Foo(Packet):
        __hdr__ = (
            ('bar', 'II', (1, 2)),
        )

    foo = Foo()
    b = bytes(foo)
    assert b == b'\x00\x00\x00\x01\x00\x00\x00\x02'


def test_unpacking_failure():
    # during dynamic-sized unpacking in the subclass there may be struct.errors raised,
    # but if the header has unpacked correctly, a different error is raised by the superclass
    import pytest

    class TestPacket(Packet):
        __hdr__ = (('test', 'B', 0),)

        def unpack(self, buf):
            Packet.unpack(self, buf)
            self.attribute = struct.unpack('B', buf[1:])

    with pytest.raises(UnpackError, match="invalid TestPacket: "):
        TestPacket(b'\x00')  # header will unpack successfully


def test_repr():
    class TestPacket(Packet):
        __hdr__ = (('_a_b', 'B', 0),)

        __bit_fields__ = {
            '_a_b': [
                ('a', 4),
                ('b', 4),
            ],
        }

    # default values so no output
    test_packet = TestPacket()
    assert repr(test_packet) == "TestPacket()"

    # non-default values
    test_packet = TestPacket(b'\x12')
    assert repr(test_packet) == "TestPacket(a=1, b=2)"
