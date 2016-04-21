#!/usr/bin/env python
#
# Copyright 2014 Matthew Wall
# See the file LICENSE.txt for your rights.

"""Driver for CC3000 data logger

http://www.rainwise.com/products/attachments/6832/20110518125531.pdf

There are a few variants:

CC-3000_ - __
       |   |
       |   41 = 418 MHz
       |   42 = 433 MHz
       |   __ = 2.4 GHz (LR compatible)
       R = serial (RS232, RS485)
       _ = USB 2.0

The CC3000 communicates using FTDI USB serial bridge.  The CC3000R has both
RS-232 and RS-485 serial ports, only one of which may be used at a time.
A long range (LR) version transmits up to 2 km using 2.4GHz.

The instrument cluster contains a DIP switch controls with value 0-3 and a
default of 0.  This setting prevents interference when there are multiple
weather stations within radio range.

The CC3000 includes a temperature sensor - that is the source of inTemp.  The
manual indicates that the CC3000 should run for 3 or 4 hours before applying
any calibration to offset the heat generated by CC3000 electronics.

The CC3000 uses 4 AA batteries to maintain its clock.  Use only rechargeable
NiMH batteries.

The logger contains 2MB or memory, with a capacity of 49834 records (over 11
months of data at a 10 minute logging interval).  The exact capacity depends
on the sensors; the basic sensor record is 42 bytes.

This driver does not support hardware record_generation.  It does support
catchup on startup.

If you request many history records then interrupt the receive, the logger will
continue to send history records until it sends all that were requested.  As a
result, any queries made while the logger is still sending will fail.

This driver was tested with a CC3000 with firmware: 1.3 Build 006 Sep 04 2013
"""

# FIXME: do a partial read of memory based on interval size
# FIXME: support non-fixed interval size

from __future__ import with_statement
import datetime
import serial
import string
import syslog
import time

from weeutil.weeutil import to_bool
import weewx.drivers

DRIVER_NAME = 'CC3000'
DRIVER_VERSION = '0.10'

def loader(config_dict, engine):
    return CC3000Driver(**config_dict[DRIVER_NAME])

def configurator_loader(config_dict):
    return CC3000Configurator()

def confeditor_loader():
    return CC3000ConfEditor()

DEBUG_SERIAL = 0
DEBUG_CHECKSUM = 0
DEBUG_OPENCLOSE = 0

def logmsg(level, msg):
    syslog.syslog(level, 'cc3000: %s' % msg)

def logdbg(msg):
    logmsg(syslog.LOG_DEBUG, msg)

def loginf(msg):
    logmsg(syslog.LOG_INFO, msg)

def logerr(msg):
    logmsg(syslog.LOG_ERR, msg)

class ChecksumMismatch(weewx.WeeWxIOError):
    def __init__(self, a, b, buf=None):
        msg = "Checksum mismatch: 0x%04x != 0x%04x" % (a,b)
        if buf is not None:
            msg = "%s (%s)" % (msg, _fmt(buf))
        weewx.WeeWxIOError.__init__(self, msg)


class CC3000Configurator(weewx.drivers.AbstractConfigurator):
    def add_options(self, parser):
        super(CC3000Configurator, self).add_options(parser)
        parser.add_option("--info", dest="info", action="store_true",
                          help="display weather station configuration")
        parser.add_option("--current", dest="current", action="store_true",
                          help="display current weather readings")
        parser.add_option("--history", dest="nrecords", type=int, metavar="N",
                          help="display N records (0 for all records)")
        parser.add_option("--history-since", dest="nminutes",
                          type=int, metavar="N",
                          help="display records since N minutes ago")
        parser.add_option("--clear-memory", dest="clear", action="store_true",
                          help="clear station memory")
        parser.add_option("--get-clock", dest="getclock", action="store_true",
                          help="display station clock")
        parser.add_option("--set-clock", dest="setclock", action="store_true",
                          help="set station clock to computer time")
        parser.add_option("--get-interval", dest="getint", action="store_true",
                          help="display logger archive interval")
        parser.add_option("--set-interval", dest="interval", metavar="N",
                          type=int,
                          help="set logging interval to N minutes (0-60)")
        parser.add_option("--get-units", dest="getunits", action="store_true",
                          help="show units of logger")
        parser.add_option("--set-units", dest="units", metavar="UNITS",
                          help="set units to METRIC or ENGLISH")
        parser.add_option('--get-dst', dest='getdst', action='store_true',
                          help='display daylight savings settings')
        parser.add_option('--set-dst', dest='dst',
                          metavar='mm/dd HH:MM,mm/dd HH:MM,MM',
                          help='set daylight savings start, end, and amount')

    def do_options(self, options, parser, config_dict, prompt):
        self.driver = CC3000Driver(**config_dict[DRIVER_NAME])
        if options.current:
            print self.driver.get_current()
        elif options.nrecords is not None:
            for r in self.driver.gen_records(nrecords):
                print r
        elif options.clear:
            self.clear_memory(prompt)
        elif options.getclock:
            print self.driver.get_time()
        elif options.setclock:
            self.set_clock(prompt)
        elif options.getdst:
            print self.driver.get_dst()
        elif options.dst is not None:
            self.set_dst(options.setdst, prompt)
        elif options.getint:
            print self.driver.get_interval()
        elif options.interval is not None:
            self.set_interval(options.interval, prompt)
        elif options.units is not None:
            self.set_units(options.units, prompt)
        else:
            print "firmware:", self.driver.get_version()
            print "time:", self.driver.get_time()
            print "dst:", self.driver.get_dst()
            print "units:", self.driver.get_units()
            print "memory:", self.driver.get_status()
            print "interval:", self.driver.get_interval()
            print "channel:", self.driver.get_channel()
            print "charger:", self.driver.get_charger()
        self.driver.closePort()

    def clear_memory(self, prompt):
        ans = None
        while ans not in ['y', 'n']:
            print self.driver.get_status()
            if prompt:
                ans = raw_input("Clear console memory (y/n)? ")
            else:
                print 'Clearing console memory'
                ans = 'y'
            if ans == 'y':
                self.driver.clear_memory()
                print self.driver.get_status()
            elif ans == 'n':
                print "Clear memory cancelled."

    def set_interval(self, interval, prompt):
        if interval < 0 or 60 < interval:
            raise ValueError("Logger interval must be 0-60 minutes")
        ans = None
        while ans not in ['y', 'n']:
            print "Interval is", self.driver.get_interval()
            if prompt:
                ans = raw_input("Set interval to %d minutes (y/n)? " % interval)
            else:
                print "Setting interval to %d minutes" % interval
                ans = 'y'
            if ans == 'y':
                self.driver.set_interval(interval)
                print "Interval is now", self.driver.get_interval()
            elif ans == 'n':
                print "Set interval cancelled."

    def set_clock(self, prompt):
        ans = None
        while ans not in ['y', 'n']:
            print "Station clock is", self.driver.get_time()
            now = datetime.datetime.now()
            if prompt:
                ans = raw_input("Set station clock to %s (y/n)? " % now)
            else:
                print "Setting station clock to %s" % now
                ans = 'y'
            if ans == 'y':
                self.driver.set_time()
                print "Station clock is now", self.driver.get_time()
            elif ans == 'n':
                print "Set clock cancelled."

    def set_units(self, units, prompt):
        if units.lower() not in ['metric', 'english']:
            raise ValueError("Units must be METRIC or ENGLISH")
        ans = None
        while ans not in ['y', 'n']:
            print "Station units is", self.driver.get_units()
            if prompt:
                ans = raw_input("Set station units to %s (y/n)? " % units)
            else:
                print "Setting station units to %s" % units
                ans = 'y'
            if ans == 'y':
                self.driver.set_units(units)
                print "Station units is now", self.driver.get_units()
            elif ans == 'n':
                print "Set units cancelled."

    def set_dst(self, dst, prompt):
        if dst != '0' and len(dst.split(',')) != 3:
            raise ValueError("DST must be 0 (disabled) or start, stop, amount "
                             "with the format mm/dd HH:MM, mm/dd HH:MM, MM")
        ans = None
        while ans not in ['y', 'n']:
            print "Station DST is", self.driver.get_dst()
            if prompt:
                ans = raw_input("Set DST to %s (y/n)? " % dst)
            else:
                print "Setting station clock to %s" % dst
                ans = 'y'
            if ans == 'y':
                self.driver.set_dst(dst)
                print "Station clock is now", self.driver.get_dst()
            elif ans == 'n':
                print "Set DST cancelled."


class CC3000Driver(weewx.drivers.AbstractDevice):
    """weewx driver that communicates with a RainWise CC3000 data logger."""

    # map rainwise names to database schema names
    DEFAULT_SENSOR_MAP = {
        'TIMESTAMP': 'TIMESTAMP',
        'TEMP OUT': 'outTemp',
        'HUMIDITY': 'outHumidity',
        'WIND DIRECTION': 'windDir',
        'WIND SPEED': 'windSpeed',
        'WIND GUST': 'windGust',
        'PRESSURE': 'pressure',
        'TEMP IN': 'inTemp',
        'T1': 'extraTemp1',
        'T2': 'extraTemp2',
        'RAIN': 'day_rain_total',
        'STATION BATTERY': 'consBatteryVoltage',
        'BATTERY BACKUP': 'bkupBatteryVoltage',
        'SOLAR RADIATION': 'radiation',
        'UV INDEX': 'UV',
    }

    def __init__(self, **stn_dict):
        self.port = stn_dict.get('port', CC3000.DEFAULT_PORT)
        self.polling_interval = float(stn_dict.get('polling_interval', 1))
        self.model = stn_dict.get('model', 'CC3000')
        self.use_station_time = to_bool(stn_dict.get('use_station_time', True))
        self.max_tries = int(stn_dict.get('max_tries', 5))
        self.retry_wait = int(stn_dict.get('retry_wait', 60))
        self.sensor_map = stn_dict.get('sensor_map', self.DEFAULT_SENSOR_MAP)
        self.last_rain = None
        self.last_rain_archive = None

        loginf('driver version is %s' % DRIVER_VERSION)
        loginf('using serial port %s' % self.port)
        loginf('polling interval is %s seconds' % self.polling_interval)
        loginf('using %s time' %
               ('station' if self.use_station_time else 'computer'))

        self.station = CC3000(self.port)
        self.station.open()

        # report the station configuration
        settings = self._init_station_with_retries(
            self.station, self.max_tries, self.retry_wait)
        self.arcint = settings['arcint']
        loginf('archive_interval is %s' % self.arcint)
        self.header = settings['header']
        loginf('header is %s' % self.header)
        self.units = weewx.METRIC if settings['units'] == 'METRIC' else weewx.US
        loginf('units are %s' % settings['units'])
        loginf('channel is %s' % settings['channel'])
        loginf('charger status: %s' % settings['charger'])

        global DEBUG_SERIAL
        DEBUG_SERIAL = int(stn_dict.get('debug_serial', 0))
        global DEBUG_CHECKSUM
        DEBUG_CHECKSUM = int(stn_dict.get('debug_checksum', 0))
        global DEBUG_OPENCLOSE
        DEBUG_OPENCLOSE = int(stn_dict.get('debug_openclose', 0))

    def genLoopPackets(self):
        ntries = 0
        while ntries < self.max_tries:
            ntries += 1
            try:
                values = self.station.get_current_data()
                ntries = 0
                data = self._parse_current(
                    values, self.header, self.sensor_map)
                ts = data.get('TIMESTAMP')
                if ts is not None:
                    if not self.use_station_time:
                        ts = int(time.time() + 0.5)
                    packet = {'dateTime': ts, 'usUnits': self.units}
                    packet.update(data)
                    packet['rain'] = self._rain_total_to_delta(
                        data['day_rain_total'], self.last_rain)
                    self.last_rain = data['day_rain_total']
                    yield packet
                if self.polling_interval:
                    time.sleep(self.polling_interval)
            except (serial.serialutil.SerialException, weewx.WeeWxIOError), e:
                logerr("Failed attempt %d of %d to get data: %s" %
                       (ntries, self.max_tries, e))
                logdbg("Waiting %d seconds before retry" % self.retry_wait)
                time.sleep(self.retry_wait)
        else:
            msg = "Max retries (%d) exceeded" % self.max_tries
            logerr(msg)
            raise weewx.RetriesExceeded(msg)

    def genStartupRecords(self, since_ts):
        """Return archive records from the data logger.  Download all records
        then return the subset since the indicated timestamp.

        Assumptions:
         - the units are consistent for the entire history.
         - the archive interval is constant for entire history.
         - the HDR for archive records is the same as current HDR
        """
        logdbg("genStartupRecords: since_ts=%s" % since_ts)
        nrec = 0
        # figure out how many records we need to download
        if since_ts is not None:
            delta = int(time.time()) - since_ts
            nrec = int(delta / self.arcint)
            logdbg("genStartupRecords: nrec=%d delta=%d" % (nrec, delta))
            if nrec == 0:
                return
        else:
            logdbg("genStartupRecords: nrec=%d" % nrec)

        totrec = int(self.station.get_memory_status().split(',')[1].split()[0])
        loginf("download %d of %d records" % (nrec, totrec))
        i = 0
        for r in self.station.gen_records(nrec):
            i += 1
            if i % 100 == 0:
                loginf("record %d of %d (total=%d)" % (i, nrec, totrec))
            if r[0] != 'REC':
                logdbg("non REC item: %s" % r[0])
                continue
            data = self._parse_historical(r[1:], self.header, self.sensor_map)
            if data['TIMESTAMP'] > since_ts:
                packet = {'dateTime': data['TIMESTAMP'],
                          'usUnits': self.units,
                          'interval': self.arcint}
                packet.update(data)
                # FIXME: is archive rain delta or total?
                packet['rain'] = self._rain_total_to_delta(
                    data['day_rain_total'], self.last_rain_archive)
                self.last_rain_archive = data['day_rain_total']
                yield packet

    @property
    def hardware_name(self):
        return self.model

    @property
    def archive_interval(self):
        return self.arcint

    def getTime(self):
        v = self.station.get_time()
        return _to_ts(v)

    def setTime(self):
        self.station.set_time()

    @staticmethod
    def _init_station_with_retries(station, max_tries, retry_wait):
        for cnt in xrange(max_tries):
            try:
                return CC3000Driver._init_station(station)
            except (serial.serialutil.SerialException, weewx.WeeWxIOError), e:
                logerr("Failed attempt %d of %d to initialize station: %s" %
                       (cnt + 1, max_tries, e))
                logdbg("Waiting %d seconds before retry" % retry_wait)
                time.sleep(retry_wait)
        else:
            raise weewx.RetriesExceeded("Max retries (%d) exceeded while initializing station" % max_tries)

    @staticmethod
    def _init_station(station):
        settings = dict()
        station.flush()
        station.set_echo()
        settings['arcint'] = 60 * station.get_interval() # arcint is in seconds
        settings['header'] = CC3000Driver._parse_header(station.get_header())
        settings['units'] = station.get_units()
        settings['channel'] = station.get_channel()
        settings['charger'] = station.get_charger()
        return settings

    @staticmethod
    def _rain_total_to_delta(rain_total, last_rain):
        # calculate the rain delta from rain total
        rain_delta = None
        if last_rain is not None:
            tmp_total = rain_total
            if tmp_total < last_rain:
                loginf("rain counter rollover detected: new=%s last=%s" %
                       (tmp_total, last_rain))
                tmp_total += 65536
            rain_delta = (tmp_total - last_rain)
        else:
            loginf("rain skipped for rain_total=%s: no last rain measurement" %
                   rain_total)
        return rain_delta

    @staticmethod
    def _parse_current(values, header, sensor_map):
        return CC3000Driver._parse_values(values, "%Y/%m/%d %H:%M:%S")

    @staticmethod
    def _parse_historical(values, header, sensor_map):
        return CC3000Driver._parse_values(values, "%Y/%m/%d %H:%M")

    @staticmethod
    def _parse_values(values, header, sensor_map, fmt):
        data = dict()
        for i, v in enumerate(values):
            if i >= len(header):
                continue
            label = sensor_map.get(header[i])
            if label is None:
                continue
            if label == 'TIMESTAMP':
                data[label] = _to_ts(v, fmt)
            else:
                data[label] = float(v)
        return data

    @staticmethod
    def _parse_header(header):
        h = []
        for v in header:
            if v == 'HDR' or v[0:1] == '!':
                continue
            h.append(v.replace('"', ''))
        return h

    # accessor methods for the configurator.  these are mostly pass-through.

    def get_current(self):
        data = self.station.get_current_data()
        return self._parse_current(data, self.header, self.sensor_map)

    def gen_records(self, nrec):
        return self.station.gen_records(nrec)

    def get_time(self):
        return self.station.get_time()

    def set_time(self):
        self.station.set_time()

    def get_dst(self):
        return self.station.get_dst()

    def set_dst(self, dst):
        self.station.set_dst(dst)

    def get_units(self):
        return self.station.get_units()

    def set_units(self, units):
        self.station.set_units(units)

    def get_interval(self):
        return self.station.get_interval()

    def set_interval(self, interval):
        self.station.set_interval(interval)

    def clear_memory(self):
        self.station.clear_memory()

    def get_version(self):
        return self.station.get_version()

    def get_status(self):
        return self.station.get_memory_status()


def _to_ts(tstr, fmt="%Y/%m/%d %H:%M:%S"):
    return time.mktime(time.strptime(tstr, fmt))

def _format_bytes(buf):
    return ' '.join(["%0.2X" % ord(c) for c in buf])

def _fmt(buf):
    return filter(lambda x: x in string.printable, buf)

# calculate the crc for a string using CRC-16-CCITT
# http://bytes.com/topic/python/insights/887357-python-check-crc-frame-crc-16-ccitt
def _crc16(data):
    reg = 0x0000
    data += '\x00\x00'
    for byte in data:
        mask = 0x80
        while mask > 0:
            reg <<= 1
            if ord(byte) & mask:
                reg += 1
            mask >>= 1
            if reg > 0xffff:
                reg &= 0xffff
                reg ^= 0x1021
    return reg

def _check_crc(buf):
    idx = buf.find('!')
    if idx < 0:
        return
    cs = buf[idx+1:idx+5]
    if DEBUG_CHECKSUM:
        logdbg("found checksum at %d: %s" % (idx, cs))
    a = _crc16(buf[0:idx]) # calculate checksum
    if DEBUG_CHECKSUM:
        logdbg("calculated checksum %x" % a)
    b = int(cs, 16) # checksum provided in data
    if a != b:
        raise ChecksumMismatch(a, b, buf)

# for some reason we sometimes get null characters randomly mixed in with the
# bytes we receive.  strip them out and let the checksum do the validation of
# the data integrity.
def _strip_unprintables(buf):
    newbuf = ''
    for x in buf:
        if x in string.printable:
            newbuf += x
    return newbuf

class CC3000(object):
    DEFAULT_PORT = '/dev/ttyUSB0'

    def __init__(self, port):
        self.port = port
        self.baudrate = 115200
        self.timeout = 5 # seconds.  clear memory takes 4 seconds.
        self.serial_port = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, _, value, traceback):
        self.close()

    def open(self):
        if DEBUG_OPENCLOSE:
            logdbg("open serial port %s" % self.port)
        self.serial_port = serial.Serial(self.port, self.baudrate,
                                         timeout=self.timeout)

    def close(self):
        if self.serial_port is not None:
            if DEBUG_OPENCLOSE:
                logdbg("close serial port %s" % self.port)
            self.serial_port.close()
            self.serial_port = None

    def write(self, data):
        if DEBUG_SERIAL:
            logdbg("write: '%s'" % data)
        n = self.serial_port.write(data)
        if n is not None and n != len(data):
            raise weewx.WeeWxIOError("Write expected %d chars, sent %d" %
                                     (len(data), n))

    def read(self):
        """The station sends CR NL before and after any response.  Some
        responses have a 4-byte CRC checksum at the end, indicated with an
        exclamation.  Not every response has a checksum.
        """
        data = self.serial_port.readline()
        if DEBUG_SERIAL:
            logdbg("read: '%s' (%s)" % (_fmt(data), _format_bytes(data)))
        data = data.strip()
        data = _strip_unprintables(data) # eliminate random NULL characters
        _check_crc(data)
        return data

    def flush(self):
        self.flush_input()
        self.flush_output()

    def flush_input(self):
        logdbg("flush input buffer")
        self.serial_port.flushInput()

    def flush_output(self):
        logdbg("flush output buffer")
        self.serial_port.flushOutput()

    def queued_bytes(self):
        return self.serial_port.inWaiting()

    def send_cmd(self, cmd):
        """Any command must be terminated with a CR"""
        self.write("%s\r" % cmd)

    def command(self, cmd):
        self.send_cmd(cmd)
        data = self.read()
        if data != cmd:
            raise weewx.WeeWxIOError("Command failed: cmd='%s' reply='%s' (%s)"
                                     % (cmd, _fmt(data), _format_bytes(data)))
        return self.read()

    def get_version(self):
        logdbg("get firmware version")
        return self.command("VERSION")

    def set_echo(self, cmd='ON'):
        logdbg("set echo to %s" % cmd)
        data = self.command('ECHO=%s' % cmd)
        if data != 'OK':
            raise weewx.WeeWxIOError("Set ECHO failed: %s" % _fmt(data))

    def get_header(self):
        logdbg("get header")
        data = self.command("HEADER")
        cols = data.split(',')
        if cols[0] != 'HDR':
            raise weewx.WeeWxIOError("Expected HDR, got %s" % cols[0])
        return cols

    def get_current_data(self):
        data = self.command("NOW")
        if data == 'NO DATA' or data == 'NO DATA RECEIVED':
            loginf("No data from sensors")
            return []
        return data.split(',')

    def get_time(self):
        # unlike all of the other accessor methods, the TIME command returns
        # OK after it returns the requested parameter.  so we have to pop the
        # OK off the serial so it does not trip up other commands.
        logdbg("get time")
        tstr = self.command("TIME=?")
        if tstr not in ['ERROR', 'OK']:
            data = self.read()
        if data != 'OK':
            raise weewx.WeeWxIOError("Failed to get time: %s" % _fmt(data))
        return tstr

    def set_time(self):
        ts = time.time()
        tstr = time.strftime("%Y/%m/%d %H:%M:%S", time.localtime(ts))
        logdbg("set time to %s (%s)" % (tstr, ts))
        s = "TIME=%s" % tstr
        data = self.command(s)
        if data != 'OK':
            raise weewx.WeeWxIOError("Failed to set time to %s: %s" %
                                     (s, _fmt(data)))

    def get_dst(self):
        logdbg("get daylight saving")
        return self.command("DST=?")

    def set_dst(self, dst):
        logdbg("set DST to %s" % dst)
        data = self.command("DST=%s" % dst)
        if data != 'OK':
            raise weewx.WeeWxIOError("Failed to set DST to %s: %s" %
                                     (dst, _fmt(data)))

    def get_units(self):
        logdbg("get units")
        return self.command("UNITS=?")

    def set_units(self, units):
        logdbg("set units to %s" % units)
        data = self.command("UNITS=%s" % units)
        if data != 'OK':
            raise weewx.WeeWxIOError("Failed to set units to %s: %s" %
                                     (units, _fmt(data)))

    def get_interval(self):
        logdbg("get logging interval")
        return int(self.command("LOGINT=?"))

    def set_interval(self, interval=5):
        logdbg("set logging interval to %d minutes" % interval)
        data = self.command("LOGINT=%d" % interval)
        if data != 'OK':
            raise weewx.WeeWxIOError("Failed to set logging interval: %s" %
                                     _fmt(data))

    def get_channel(self):
        logdbg("get channel")
        return self.command("STATION")

    def set_channel(self, channel):
        logdbg("set channel to %d" % channel)
        if channel < 0 or 3 < channel:
            raise ValueError("Channel must be 0-3")
        data = self.command("STATION=%d" % channel)
        if data != 'OK':
            raise weewx.WeeWxIOError("Failed to set channel: %s" % _fmt(data))

    def get_charger(self):
        logdbg("get charger")
        return self.command("CHARGER")

    def get_memory_status(self):
        logdbg("get memory status")
        return self.command("MEM=?")

    def clear_memory(self):
        logdbg("clear memory")
        data = self.command("MEM=CLEAR")
        if data != 'OK':
            raise weewx.WeeWxIOError("Failed to clear memory: %s" % _fmt(data))

    def gen_records(self, nrec=0):
        """generator function for getting nrec records from the device"""

        logdbg("download %s records" % nrec)
        need_cmd = True
        cmd_max = 5
        cmd_cnt = 0
        n = 0
        while True:
            if need_cmd:
                if cmd_cnt >= cmd_max:
                    logerr("download aborted after %d attempts" % cmd_max)
                    break
                cmd_cnt += 1
                qty = nrec - n if nrec else 0 # FIXME
                logdbg("download attempt %s of %s" % (cmd_cnt, cmd_max))
                cmd = "DOWNLOAD=%d" % qty if qty else "DOWNLOAD"
                self.send_cmd(cmd)
                need_cmd = False
            try:
                data = self.read()
                if data == 'OK':
                    logdbg("end of records")
                    break
                values = data.split(',')
                if values[0] == 'REC':
                    logdbg("record %d" % n)
                    n += 1
                    cmd_cnt = 0
                    yield values
                elif (values[0] == 'HDR' or values[0] == 'MSG' or
                      values[0].startswith('DOWNLOAD')):
                    pass
                elif values[0] == '':
                    # FIXME: this causes 'input overrun' on rpi2 with debian 7
                    logdbg("download hung, initiate another download")
                    need_cmd = True
                else:
                    logerr("bad record %s '%s' (%s)" %
                           (n, _fmt(values[0]), _fmt(data)))
            except ChecksumMismatch, e:
                logerr("record failed: %s" % e)


class CC3000ConfEditor(weewx.drivers.AbstractConfEditor):
    @property
    def default_stanza(self):
        return """
[CC3000]
    # This section is for RainWise MarkIII weather stations and CC3000 logger.

    # Serial port such as /dev/ttyS0, /dev/ttyUSB0, or /dev/cuaU0
    port = %s

    # The station model, e.g., CC3000 or CC3000R
    model = CC3000

    # The driver to use:
    driver = weewx.drivers.cc3000
""" % (CC3000.DEFAULT_PORT,)

    def prompt_for_settings(self):
        print "Specify the serial port on which the station is connected, for"
        print "example /dev/ttyUSB0 or /dev/ttyS0."
        port = self._prompt('port', CC3000.DEFAULT_PORT)
        return {'port': port}


# define a main entry point for basic testing without weewx engine and service
# overhead.  invoke this as follows from the weewx root dir:
#
# PYTHONPATH=bin python bin/weewx/drivers/cc3000.py

if __name__ == '__main__':
    import optparse

    usage = """%prog [options] [--help]"""

    syslog.openlog('cc3000', syslog.LOG_PID | syslog.LOG_CONS)
    syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_INFO))
    parser = optparse.OptionParser(usage=usage)
    parser.add_option('--version', dest='version', action='store_true',
                      help='display driver version')
    parser.add_option('--debug', dest='debug', action='store_true',
                      default=False,
                      help='emit additional diagnostic information')
    parser.add_option('--test-crc', dest='testcrc', action='store_true',
                      help='test crc')
    parser.add_option('--port', dest='port', metavar='PORT',
                      help='port to which the station is connected',
                      default=CC3000.DEFAULT_PORT)
    parser.add_option('--get-version', dest='getver', action='store_true',
                      help='display firmware version')
    parser.add_option('--get-status', dest='status', action='store_true',
                      help='display memory status')
    parser.add_option('--get-channel', dest='getch', action='store_true',
                      help='display station channel')
    parser.add_option('--get-battery', dest='getbat', action='store_true',
                      help='display battery status')
    parser.add_option('--get-current', dest='getcur', action='store_true',
                      help='display current data')
    parser.add_option('--get-memory', dest='getmem', action='store_true',
                      help='display memory status')
    parser.add_option('--get-records', dest='getrec', metavar='NUM_RECORDS',
                      help='display records from station memory')
    parser.add_option('--get-header', dest='gethead', action='store_true',
                      help='display data header')
    parser.add_option('--get-units', dest='getunits', action='store_true',
                      help='display units')
    parser.add_option('--set-units', dest='setunits', metavar='UNITS',
                      help='set units to ENGLISH or METRIC')
    parser.add_option('--get-time', dest='gettime', action='store_true',
                      help='display station time')
    parser.add_option('--set-time', dest='settime', action='store_true',
                      help='set station time to computer time')
    parser.add_option('--get-dst', dest='getdst', action='store_true',
                      help='display daylight savings settings')
    parser.add_option('--set-dst', dest='setdst',
                      metavar='mm/dd HH:MM,mm/dd HH:MM,MM',
                      help='set daylight savings start, end, and amount')
    parser.add_option('--get-interval', dest='getint', action='store_true',
                      help='display logging interval, in seconds')
    parser.add_option('--set-interval', dest='setint', metavar='INTERVAL',
                      help='set logging interval, in seconds')
    parser.add_option('--clear-memory', dest='clear', action='store_true',
                      help='clear logger memory')
    (options, args) = parser.parse_args()

    if options.version:
        print "CC3000 driver version %s" % DRIVER_VERSION
        exit(0)

    if options.debug:
        DEBUG_SERIAL = 1
        DEBUG_CHECKSUM = 1
        DEBUG_OPENCLOSE = 1
        syslog.setlogmask(syslog.LOG_UPTO(syslog.LOG_DEBUG))

    if options.testcrc:
        _check_crc('OK')
        _check_crc('REC,2010/01/01 14:12, 64.5, 85,29.04,349,  2.4,  4.2,  0.00, 6.21, 0.25, 73.2,!B82C')
        _check_crc('MSG,2010/01/01 20:22,CHARGER ON,!4CED')
        exit(0)

    with CC3000(options.port) as s:
        if options.getver:
            print s.get_version()
        if options.status:
            print "firmware:", s.get_version()
            print "time:", s.get_time()
            print "dst:", s.get_dst()
            print "units:", s.get_units()
            print "memory:", s.get_memory_status()
            print "interval:", s.get_interval()
            print "channel:", s.get_channel()
            print "charger:", s.get_charger()
        if options.getch:
            print s.get_channel()
        if options.getbat:
            print s.get_charger()
        if options.getcur:
            print s.get_current_data()
        if options.getmem:
            print s.get_memory_status()
        if options.getrec is not None:
            i = 0
            for r in s.gen_records(int(options.getrec)):
                print i, r
                i += 1
        if options.gethead:
            print s.get_header()
        if options.getunits:
            print s.get_units()
        if options.setunits:
            s.set_units(options.setunits)
        if options.gettime:
            print s.get_time()
        if options.settime:
            s.set_time()
        if options.getdst:
            print s.get_dst()
        if options.setdst:
            s.set_dst(options.setdst)
        if options.getint:
            print s.get_interval()
        if options.setint:
            s.set_interval(int(options.setint))
        if options.clear:
            s.clear_memory()
