
import binascii
import ConfigParser
import math
import MySQLdb
import os
import time
import socket

from ..icea.icea_protocol import Icea

from ... import core_utils
from ... import emane_utils
from ... import geo_utils


# -----------------------------------------------------------------------------
# class Radar
# -----------------------------------------------------------------------------
class Radar:

    """
    This class emulates the functionality of the radar
    """

    M_TO_FT = 3.28084
    M_TO_NM = 0.000539957
    MPS_TO_KT = 1.94384
    DEG_SETOR = 11.25

    # PSR default values
    sweep_time = 4.0

    # Default values
    net_ip = "172.18.104.255"
    net_port = 65000
    net_mode = "broadcast"
    net_proto = "ICEA"

    db_name = 'atn_sim'
    db_user = 'atn_sim'
    db_pass = 'atn_sim'
    db_host = '172.17.255.254'


    # -------------------------------------------------------------------------
    # method constructor
    # -------------------------------------------------------------------------
    def __init__(self, config="radar.cfg"):
        """
        Initialize attributes of class
        """

        # Node name of radar in simulation
        self.nodename = core_utils.get_node_name()

        # Node number of radar in simulation
        self.nodenumber = core_utils.get_node_number()

        # Simulation ID
        self.session_id = core_utils.get_session_id()

        # NEM ID
        self.nemid = core_utils.get_nem_id(node_name=self.nodename, session_id=self.session_id)

        self.nodenames = {}
        self.nodenumbers = {}

        # Reading configuration file
        if os.path.exists(config):
            print "Configuration file %s found." % config
            conf = ConfigParser.ConfigParser()
            conf.read(config)

            self.latitude  = conf.getfloat("Location", "latitude")
            self.longitude = conf.getfloat("Location", "longitude")
            self.altitude  = conf.getfloat("Location", "altitude")

            # Radar parameters
            self.sweep_time = conf.getfloat("PSR", "sweep_time")

            # Network parameters
            self.net_ip    = conf.get("Network", "destination")
            self.net_port  = conf.getint("Network", "port")
            self.net_mode  = conf.get("Network", "mode")
            self.net_proto = conf.get("Network", "protocol")

            # Placing radar on the proper location
            # set_location(nemid, lat, lon, alt, heading, speed, climb)
            emane_utils.set_location(nemid=self.nemid, lat=self.latitude, lon=self.longitude, alt=self.altitude,
                                     heading=0.0, speed=0.0, climb=0.0)
        else:
            location = emane_utils.get_nem_location(nem_id=self.nemid)

            self.latitude = location["latitude"]
            self.longitude = location["longitude"]
            self.altitude = location["altitude"]

        if self.net_proto == "ICEA":
            self.encoder = Icea()

            # Pre-calculating for speed
            self.empty_msg = {}
            for sector in range(0, 32):
                self.empty_msg[sector] = self.encoder.get_empty_sector_msg(sector)
        else:
            print "Radar protocol %s is not supported." % self.net_proto
            self.encoder = None

        # DB connection with general purposes
        self.db = MySQLdb.connect(self.db_host, self.db_user, self.db_pass, self.db_name)

        # DB connection specific for thread _process_msg()
        self.db_process = MySQLdb.connect(self.db_host, self.db_user, self.db_pass, self.db_name)

        print "Location of Radar:"
        print "  Latitude:  %f" % self.latitude
        print "  Longitude: %f" % self.longitude
        print "  Altitude:  %f m" % self.altitude
        print "Network settings:"
        print "  Destination:  %s" % self.net_ip
        print "  Port:         %s" % self.net_port
        print "  Mode:         %s" % self.net_mode
        print "  Radar Proto.: %s" % self.net_proto


    # -------------------------------------------------------------------------
    # method start
    # -------------------------------------------------------------------------
    def start(self):
        """method start

        This method has the cyclic function 4/2 for aircraft that are within
        the radar coverage and send in broadcast mode (UDP) network
        """

        while True:

            t0 = time.time()

            tracks = self.detect()

            print "Objects detected: %d" % len(tracks)
            for t in tracks:
                print "  > %s: %f %f %f %f %f %s" % (t[7], t[1], t[2], t[3], t[4], t[5], t[8])

            self.broadcast(tracks)

            t1 = time.time()

            # Processing time
            ptime = t1 - t0

            print "========"

            time.sleep(radar.sweep_time - ptime)

            t2 = time.time()

            print t2-t0


    # -------------------------------------------------------------------------
    # method detect - aircraft(s) on the sector actual
    # -------------------------------------------------------------------------
    def detect(self):
        """method detect

        This method  has the function of detecting  moving objects within CORE,
        apply detection filters (horizontal and vertical) and finally generate
        a list of targets that are within the radar coverage.
        """

        detected_tracks = []

        for i in emane_utils.get_all_locations():

            nemid = i["nem"]
            lat = i["latitude"]
            lon = i["longitude"]
            alt = i["altitude"]

            if nemid not in self.nodenames:
                self.nodenames[nemid] = core_utils.get_node_name(nem_id=nemid, session_id=self.session_id)
                self.nodenumbers[nemid] = core_utils.get_node_number(node_name=self.nodenames[nemid],
                                                                     session_id=self.session_id)

            nodenum = self.nodenumbers[nemid]

            # Prevent radar to detect itself
            if nodenum == self.nodenumber:
                continue

            x, y = self.calc_distance_from(lat, lon, alt)

            z = float(alt)

            # (azimuth, elevation, magnitude) = core_utils.get_node_velocity(nemid)

            azimuth = i["azimuth"]
            elevation = i["elevation"]
            magnitude = i["magnitude"]

            # Climb rate (in m/s)
            climb_rate = float(magnitude) * math.sin(math.radians(float(elevation)))

            # Speed (in m/s)
            speed = float(magnitude) * math.cos(math.radians(float(elevation)))

            # SSR and Callsign
            callsign, ssr = self.read_transponder(nemid)

            # Sector
            h_angle = RadarUtils.calc_horizontal_angle(x, y, z)

            sector = int(h_angle / self.DEG_SETOR)

            # ToDo: how to encode primary radar plots?
            if callsign is not None and ssr is not None:
                detected_tracks.append([nodenum,
                                        x * self.M_TO_NM,
                                        y * self.M_TO_NM,
                                        z * self.M_TO_FT,
                                        azimuth,
                                        speed * self.MPS_TO_KT,
                                        ssr,
                                        callsign,
                                        sector])

        return detected_tracks

    def read_transponder(self, nemid):
        #
        # Read transponder's parameters using database shared memory
        #
        # ToDo: read this information from aircraft transponder
        try:
            cursor = self.db_process.cursor()
            cursor.execute("SELECT callsign, performance_type, ssr_squawk FROM transponder WHERE nem_id=%d" % nemid)
            result = cursor.fetchone()

            if result is not None:

                db_callsign = result[0]
                db_perftype = result[1]
                db_squawk = result[2]

                cursor.close()

                return db_callsign, db_squawk

        except MySQLdb.Error, e:
            print "read_transponder(): MySQL Error [%d]: %s" % (e.args[0], e.args[1])

        return None, None

    # -------------------------------------------------------------------------
    # method broadcast
    # -------------------------------------------------------------------------
    def broadcast(self, tracks):
        """method broadcast

        This method sends first message empty sector network and seeks to identify
        the aircraft that are within the current sector, formats the messages from
        the aircraft to the icea protocol to be sent on the network.
        """

        # get aircraft(s) of sector actual
        messages = self.encoder.encode_icea(tracks)

        if messages is None:
            return

        for sector in range(0, 32):

            # send message empty sector
            self.net_send(self.empty_msg[sector])

            for i in range(0, len(messages)):

                # sectors iquals
                if tracks[i][-1] == sector:

                   # send message track
                   self.net_send(messages[i])


    # -------------------------------------------------------------------------
    # method calc_distance_from
    # -------------------------------------------------------------------------
    def calc_distance_from(self, lat, lon, alt):
        """method calc_distance_from

        This method calculates the distance of the aircraft on radar
        """

        x, y, z = geo_utils.geog2enu(float(lat), float(lon), float(alt), self.latitude, self.longitude, self.altitude)

        return x, y


    # -------------------------------------------------------------------------
    # method net_send
    # -------------------------------------------------------------------------
    def net_send(self, msg):
        """method net_send

        This method performs the post on the network in broadcast mode and udp
        """

        hex_msg = binascii.unhexlify(msg)

        # UDP
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        if self.net_mode == "broadcast":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

        # send msg
        try:
            sock.sendto(hex_msg, (self.net_ip, self.net_port))
        except IOError, e:
            if e.errno == 101:
                #print "Network unreachable"
                pass
        finally:
            sock.close()


# -----------------------------------------------------------------------------
# class RadarUtils
# -----------------------------------------------------------------------------
class RadarUtils:

    DEG_PI_3 = 60.0           # PI / 3
    DEG_PI_2 = 90.0           # PI / 2
    DEG_PI = 180.0          # PI
    DEG_3PI_2 = 270.0          # 3 PI / 2
    DEG_2PI = 360.0          # 2 PI
    RAD_DEG = 57.29577951    # Converte RAD para DEG
    DEG_RAD = 0.017453292    # Converte DEG para RAD
    DEG_SETOR = 11.25

    @staticmethod
    def calc_horizontal_angle(x_pos, y_pos, z_pos = None):

        # calcula a nova radial (proa de demanda)
        if x_pos > 0:
            return 90.0 - math.degrees(math.atan(y_pos / x_pos))

        if x_pos < 0:
            angle_tmp = 270.0 - math.degrees(math.atan(y_pos / x_pos))

            if angle_tmp >= 360.0:
                return angle_tmp - 360.0
            else:
                return angle_tmp

        if y_pos >= 0:
            return 0
        else:
            return RadarUtils.DEG_PI

    @staticmethod
    def calc_vertical_angle(x_pos, y_pos, z_pos):
        if z_pos == 0.0:
            return 0.0

        if x_pos == 0.0:
            return 90.0

        v_angle = math.atan(z_pos / abs(x_pos))

        if z_pos < 0:
            return -v_angle

        return v_angle


if __name__ == '__main__':

    session_id = core_utils.get_session_id()

    radar = Radar()
    radar.start()
