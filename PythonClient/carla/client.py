# Copyright (c) 2017 Computer Vision Center (CVC) at the Universitat Autonoma de
# Barcelona (UAB), and the INTEL Visual Computing Lab.
#
# This work is licensed under the terms of the MIT license.
# For a copy, see <https://opensource.org/licenses/MIT>.

"""CARLA Client."""

import struct

from contextlib import contextmanager

from . import sensor
from . import tcp
from . import util

try:
    from . import carla_server_pb2 as carla_protocol
except ImportError:
    raise RuntimeError('cannot import "carla_server_pb2.py", run the protobuf compiler to generate this file')



VehicleControl = carla_protocol.Control


@contextmanager
def make_carla_client(host, world_port, timeout=15):
    with util.make_connection(CarlaClient, host, world_port, timeout) as client:
        yield client


class CarlaClient(object):
    def __init__(self, host, world_port, timeout=15):
        self._world_client = tcp.TCPClient(host, world_port, timeout)
        self._stream_client = tcp.TCPClient(host, world_port + 1, timeout)
        self._control_client = tcp.TCPClient(host, world_port + 2, timeout)
        self._current_settings = None
        # Controls the state, if an episode is already started.
        self._is_episode_requested = False

    def connect(self):
        self._world_client.connect()

    def disconnect(self):
        self._control_client.disconnect()
        self._stream_client.disconnect()
        self._world_client.disconnect()

    def connected(self):
        return self._world_client.connected()

    def load_settings(self, carla_settings):
        """
        Load new settings and request a new episode based on these settings to
        the server. carla_settings object must be convertible to a str holding a
        CarlaSettings.ini.

        Returns a protobuf object holding the scene description.
        """
        self._current_settings = carla_settings
        return self._request_new_episode(carla_settings)


    def start_episode(self, player_start_index):
        """
        Start the new episode at the player start given by the
        player_start_index. The list of player starts is retrieved by
        "load_settings". Requests a new episode based on the last settings
        loaded by "load_settings".

        This function waits until the server answers with an EpisodeReady.
        """
        if self._current_settings is None:
            raise RuntimeError('no settings loaded, cannot start episode')

        # if no new settings are loaded, request new episode with previous
        if not self._is_episode_requested:
            self._request_new_episode(self._current_settings)

        try:
            pb_message = carla_protocol.EpisodeStart()
            pb_message.player_start_spot_index = player_start_index
            self._world_client.write(pb_message.SerializeToString())
            # Wait for EpisodeReady.
            data = self._world_client.read()
            if not data:
                raise RuntimeError('failed to read data from server')
            pb_message = carla_protocol.EpisodeReady()
            pb_message.ParseFromString(data)
            if not pb_message.ready:
                raise RuntimeError('cannot start episode: server failed to start episode')
            # We can start the agent clients now.
            self._stream_client.connect()
            self._control_client.connect()
            # Set again the status for no episode requested
        finally:
            self._is_episode_requested = False

    def read_data(self):
        """
        Read data sent from the server this frame. The episode must be started.
        Return the protobuf object with the measurements followed by the raw
        data of the sensors.
        """
        # Read measurements.
        data = self._stream_client.read()
        if not data:
            raise RuntimeError('failed to read data from server')
        pb_message = carla_protocol.Measurements()
        pb_message.ParseFromString(data)
        # Read sensor data.
        raw_sensor_data = self._stream_client.read()
        return pb_message, self._parse_raw_sensor_data(raw_sensor_data)

    def send_control(self, *args, **kwargs):
        """Send vehicle control for the current frame."""
        if isinstance(args[0] if args else None, carla_protocol.Control):
            pb_message = args[0]
        else:
            pb_message = carla_protocol.Control()
            pb_message.steer = kwargs.get('steer', 0.0)
            pb_message.throttle = kwargs.get('throttle', 0.0)
            pb_message.brake = kwargs.get('brake', 0.0)
            pb_message.hand_brake = kwargs.get('hand_brake', False)
            pb_message.reverse = kwargs.get('reverse', False)
        self._control_client.write(pb_message.SerializeToString())

    def _request_new_episode(self, carla_settings):
        """
        Request a new episode. Internal function to request information about a
        new episode episode that is going to start. It also prepare the client
        for reset by disconnecting stream and control clients.
        """
        # Disconnect agent clients.
        self._stream_client.disconnect()
        self._control_client.disconnect()
        # Send new episode request.
        pb_message = carla_protocol.RequestNewEpisode()
        pb_message.ini_file = str(carla_settings)
        self._world_client.write(pb_message.SerializeToString())
        # Read scene description.
        data = self._world_client.read()
        if not data:
            raise RuntimeError('failed to read data from server')
        pb_message = carla_protocol.SceneDescription()
        pb_message.ParseFromString(data)
        if len(pb_message.player_start_spots) < 1:
            raise RuntimeError("received 0 player start spots")
        self._is_episode_requested = True
        return pb_message

    @staticmethod
    def _parse_raw_sensor_data(raw_data):
        # At this point the only sensors available are images, the raw_data is
        # consists of images only.
        image_types = ["None", "SceneFinal", "Depth", "SemanticSegmentation"]
        gettype = lambda id: image_types[id] if len(image_types) > id else "Unknown"
        getval = lambda index: struct.unpack('<L', raw_data[index*4:index*4+4])[0]
        sensors = []
        total_size = len(raw_data) / 4
        index = 0
        while index < total_size:
            width = getval(index)
            height = getval(index + 1)
            image_type = gettype(getval(index + 2))
            begin = index + 3
            end = begin + width * height
            sensors.append(sensor.Image(width, height, image_type, raw_data[begin*4:end*4]))
            index = end
        return sensors
