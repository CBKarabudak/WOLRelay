#!/usr/bin/env python3

import datetime
import multiprocessing
import os
import json
import re
import traceback
import functools
import logging
import time

from scapy.all import sniff
from wakeonlan import send_magic_packet
from flask import Flask, request, abort, send_from_directory
from werkzeug.exceptions import NotFound

app = Flask(__name__)

scriptPath = os.path.dirname(os.path.realpath(__file__))
configPath = os.path.join(scriptPath, 'config.json')
config = open(configPath).read()
config = json.loads(config)

logging.basicConfig()
logger = logging.getLogger("WOLRelay")
logger.setLevel(config['logLevel'])

multiprocessingManager = multiprocessing.Manager()
ARPTable = multiprocessingManager.dict()

@app.before_request
def beforeRequest():
  # optionally mitigate against DNS rebinding
  if 'hosts' in config.keys():
    splitHost = request.host
    if ':' in splitHost:
      splitHost = request.host.split(':')[0]
    if splitHost != "localhost" and splitHost != "127.0.0.1": # whitelist localhost because of Docker health checks
      if splitHost not in config['hosts']:
        abort(403)

def processARP(packets):
  for packet in packets:
    if packet.type == 2054:   # only process ARP packets
        if packet.op == 2:    # only process ARP *reply* packets
          if packet.hwsrc.upper() in ARPTable.keys():   # only process packets from MAC addresses we care about
            mac = packet.hwsrc
            ip = packet.psrc
            logging.debug('IP ' + ip + ' is assigned to ' + mac + ' as of ' + datetime.datetime.now().isoformat())
            ARPTable[mac.upper()] = (ip, datetime.datetime.now())

def sniffARPPackets(interface = None):
  if interface:
    try:
      sniff(prn=processARP, iface=interface, filter="(arp[6:2] = 2)")  # run scapy with BPF for ARP packets with opcode 2
    except Exception:
      logger.warning("Running scapy in filtered mode failed, filtering without the help of Berkeley Packet Filtering. This is going to be VERY slow and unreliable. You should try installing tcpdump if you're on Linux, and Npcap if you're on Windows.")
      traceback.print_exc()
      sniff(prn=processARP)     # filtering failed, fall back to inspecting every packet
  else:
    try:
      sniff(prn=processARP, filter="(arp[6:2] = 2)")  # run scapy with BPF for ARP packets with opcode 2
    except Exception:
      logger.warning("Running scapy in filtered mode failed, filtering without the help of Berkeley Packet Filtering. This is going to be VERY slow and unreliable. You should try installing tcpdump if you're on Linux, and Npcap if you're on Windows.")
      traceback.print_exc()
      sniff(prn=processARP)     # filtering failed, fall back to inspecting every packet

def sendARPRequest(interface, destination):
    logger.debug('sending ARP request to ' + destination)
    scapy.layers.l2.arping(destination, iface=interface, timeout=0, cache=True, verbose=False)

def scanNetwork():
  while True:
    try:
      pool = multiprocessing.Pool(processes=10)
      processes = []

      for network, netmask, _, interface, address, _ in scapy.config.conf.route.routes:
        # skip loopback network and default gw
        if network == 0 or interface == 'lo' or address == '127.0.0.1' or address == '0.0.0.0':
          continue

        if netmask <= 0 or netmask == 0xFFFFFFFF:
          continue

        # skip docker interface
        if interface.startswith('docker') or interface.startswith('br-'):
          continue

        subnet = '.'.join(address.split('.')[:-1])
        IPRange = [subnet + '.' + str(i) for i in range(1, 254)]
        boundARPRequest = functools.partial(sendARPRequest, interface)
        processes.append(pool.map_async(boundARPRequest, IPRange))

      for process in processes:
        process.get()
      pool.close()
      pool.join()
    except:
      logger.warning('scanning the network failed! exception details: ' + traceback.format_exc())
    finally:
      time.sleep(config['arp']['scanInterval'])

"""
For a given MAC address, returns the IP address and the timestamp for when we recorded it.

Returns HTTP501 if ARP is disabled from the configuration file.
Returns HTTP400 if the MAC address is invalid or does not exist in our ARP table.
Returns HTTP204 if the MAC address does not have a corresponding IP address yet.

@mac MAC address to scan ARP table for. If undefined, data for all MAC addresses will be returned.
"""
@app.route('/getStatus')
def getStatus():
  mac = None
  if mac in request.args:
    mac = request.args.get('mac')
    mac = mac.upper()

  if 'arp' not in config.keys():
    return (json.dumps({"error": "ARP is disabled in the configuration file"}), 501)
  if mac:
    if mac not in ARPTable.keys():
      return (json.dumps({"error": "MAC is not defined in the configuration file"}), 400)
    if not ARPTable[mac]:
      return (json.dumps({"error": "The server does not have any information about this MAC address yet"}), 204)

    return json.dumps([{
      "IP": ARPTable[mac][0],
      "lastSeen": ARPTable[mac][1].isoformat()
    }])
  else:
    result = []
    for mac in ARPTable.keys():
      if not ARPTable[mac]:
        result.append({
          "MAC": mac,
          "IP": None,
          "lastSeen": None
        })
      else:
        result.append({
        "MAC": mac,
        "IP": ARPTable[mac][0],
        "lastSeen": ARPTable[mac][1].isoformat()
    })
    return json.dumps(result)

"""
Sends a Wake-on-LAN "magic packet" to the specified MAC address.

Returns HTTP400 if the MAC address appears to be invalid.

@mac MAC address to send packet to.
"""
@app.route('/wakeDevice', methods=['POST'])
def wakeDevice():
  mac = request.json['mac']
  mac = mac.upper()

  if not re.match("[0-9A-F]{2}([-:]?)[0-9A-F]{2}(\\1[0-9A-F]{2}){4}$", mac.lower()):
    return json.dumps({"error": "MAC address verification failed"}, 400)

  try:
    send_magic_packet(mac, ip_address=config['broadcastAddress'], port=config['broadcastPort'])
    return json.dumps({"error": None})
  except Exception:
    return (json.dumps({"error": traceback.format_exc()}), 500)

# hackity hack
# serve static files from the static directory
# this is so that the user doesn't need to configure a webserver to run and/or debug
# but it's encouraged to do so anyway for performance reasons
@app.route('/<path:path>')
def staticHost(path):
  try:
    return send_from_directory(os.path.join(scriptPath, 'static'), path)
  except NotFound as e:
    if path.endswith("/"):
      return send_from_directory(os.path.join(scriptPath, 'static'), path + "index.html")
    raise e

@app.route('/')
def staticIndex():
  return send_from_directory(os.path.join(scriptPath, 'static'), "index.html")

if __name__ == '__main__':
  if 'arp' in config.keys():
    if 'scanInterfaces' in config['arp'].keys():
      for interface in config['arp']['scanInterfaces']:
        sniffingProcess = multiprocessing.Process(target=sniffARPPackets, args=[interface])
        sniffingProcess.start()
    else:
      sniffingProcess = multiprocessing.Process(target=sniffARPPackets)
      sniffingProcess.start()

    for mac in config['arp']['macAddresses']:
      ARPTable[mac.upper()] = None

    if 'scanInterval' in config['arp'].keys():
      scanningProcess = multiprocessing.Process(target=scanNetwork)
      scanningProcess.start()

  app.run(config['localIP'], port=config['APIPort'], threaded=True)