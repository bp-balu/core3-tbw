from __init__ import __version__, __version_info__
from config.configure import Configure
from config.pool_config import PoolConfig
from network.network import Network
from utility.sql import Sql
from utility.utility import Utility
from flask import Flask, render_template
from functools import cmp_to_key
from multiprocessing import Process
from threading import Event
import datetime
import logging
import signal
import sys
import time
import requests

app = Flask(__name__)


def get_round(height):
    mod = divmod(height,network.delegates)
    return (mod[0] + int(mod[1] > 0))

FIRST_BLOCK_UNIX = None

def get_first_block_unix():
    global FIRST_BLOCK_UNIX
    if FIRST_BLOCK_UNIX is None:
        url = "http://localhost:6003/api/blocks/first"
        try:
            response = requests.get(url)
            response.raise_for_status()
            data = response.json()["data"]
            FIRST_BLOCK_UNIX = data["timestamp"]["unix"]
        except requests.exceptions.RequestException as e:
            print(f"Error retrieving first block Unix timestamp: {e}")
            return None
    return FIRST_BLOCK_UNIX

def get_reliability():
    try:
        first_block_unix = get_first_block_unix()

        today_unix = int(time.time()) - first_block_unix
        thirty_days_ago_unix = today_unix - (30 * 24 * 60 * 60)

        sql.open_connection()
        query = f"""SELECT COUNT(*) FROM blocks WHERE "timestamp" BETWEEN {thirty_days_ago_unix} AND {today_unix}"""
        produced_blocks = sql.cursor.execute(query).fetchone()[0]
        sql.close_connection()

        missed_blocks_url = "http://localhost:6003/api/blocks/missed"
        params = {"page": 1, "limit": 1, "username": config.delegate}
        response = requests.get(missed_blocks_url, params=params)
        total_missed_blocks = response.json()["meta"]["totalCount"]

        total_blocks = produced_blocks + total_missed_blocks
        reliability = "{:.2f}".format(100 - (total_missed_blocks / total_blocks) * 100)
        return reliability
    except Exception as e:
        print(f"An error occurred: {e}")
        return None

@app.route('/')
def index():
    stats = {}
    ddata = client.delegates.get(config.delegate)

    stats['forged']   = ddata['data']['blocks']['produced']
    #s['missed'] = dstats['data']['blocks']['missed']
    #s['missed'] = 0 # temp fix
    stats['rank']     = ddata['data']['rank']
    #s['productivity'] = dstats['data']['production']['productivity']
    #s['productivity'] = 100 # temp fix
    stats['handle']   = ddata['data']['username']
    stats['wallet']   = ddata['data']['address']
    stats['votes']    = "{:,.2f}".format(int(ddata['data']['votesReceived']['votes'])/config.atomic)
    stats['voters']   = int(ddata['data']['votesReceived']['voters'])
    stats['rewards']  = ddata['data']['forged']['total']
    stats['approval'] = ddata['data']['votesReceived']['percent']
   #stats['version']  = ddata['data']['version']

    # get all forged blocks in reverse chronological order, first page, max 100 as default
    dblocks = client.delegates.blocks(config.delegate) 
    stats['lastforged_no'] = dblocks['data'][0]['height']
    stats['lastforged_id'] = dblocks['data'][0]['id']
    stats['lastforged_ts'] = dblocks['data'][0]['timestamp']['human']
    stats['lastforged_unix'] = dblocks['data'][0]['timestamp']['unix']
    age = divmod(int(time.time() - stats['lastforged_unix']), 60)
    stats['lastforged_ago'] = "{0}:{1}".format(age[0],age[1])
    stats['forging'] = 'Active' if stats['rank'] <= network.delegates else 'Standby'

    sql.open_connection()
    voters = sql.all_voters().fetchall()
    voters_balance = sql.get_all_voters_last_balance().fetchall()
    sql.close_connection()

    voter_stats = []
    pend_total  = 0
    paid_total  = 0
    lbook       = dict((addr,(pend,paid)) for addr, pubkey, pend, paid, rate in voters)
    lvote       = dict((address, (balance, vbalance)) for address, balance, vbalance in voters_balance)
    votetotal   = sum(int(lvote[item][1]) for item in lvote)
    for _addr in lvote:
        if _addr in lbook:
            vbalance = int(lvote[_addr][1])
            #logger.debug(f"addr:{_addr} vbalance:{vbalance} allvotes:{votetotal} ratio:{vbalance/votetotal}")
            _sply = "{:.2f}".format(vbalance*100/votetotal) if votetotal > 0 else "-"
            voter_stats.append([_addr,"{:,.8f}".format(int(lbook[_addr][0])/config.atomic), "{:,.8f}".format(int(lbook[_addr][1])/config.atomic), "{:,.8f}".format(vbalance/config.atomic), _sply])
            pend_total += int(lbook[_addr][0])
            paid_total += int(lbook[_addr][1])

    reverse_key = cmp_to_key(lambda a, b: (a < b) - (a > b))
    voter_stats.sort(key=lambda rows: (reverse_key(rows[3]),rows[0]))
    voter_stats.insert(0,["Total", "{:,.8f}".format(pend_total/config.atomic), "{:,.8f}".format(paid_total/config.atomic), "{:,.8f}".format(votetotal/config.atomic), "100.00"])

    node_sync_data = client.node.syncing()
    stats['synced'] = 'Syncing' if node_sync_data['data']['syncing'] else 'Synced'
    stats['behind'] = node_sync_data['data']['blocks']
    stats['height'] = node_sync_data['data']['height']

    stats['reliability'] = get_reliability()

    return render_template(poolconfig.pool_template + '_index.html', node=stats, voter=voter_stats, tags=tags)


@app.route('/payments')
def payments():
    sql.open_connection()
    xactions = sql.transactions().fetchall()
    sql.close_connection()

    tx_data = []
    for i in xactions:
        data_list = [i[0], int(i[1]), i[2], i[3]]
        tx_data.append(data_list)

    return render_template(poolconfig.pool_template + '_payments.html', tx_data=tx_data, tags=tags)


# Handler for SIGINT and SIGTERM
def sighandler(signum, frame):
    global server
    logger.info("SIGNAL {0} received. Starting graceful shutdown".format(signum))
    server.kill()
    logger.info("< Terminating POOL...")
    return


if __name__ == '__main__':    
    # get configuration
    config = Configure()
    if (config.error):
        print("FATAL: config.ini not found! Terminating POOL.", file=sys.stderr)
        sys.exit(1)

    poolconfig = PoolConfig()
    if (poolconfig.error):
        print("FATAL: pool_config.ini not found! Terminating POOL.", file=sys.stderr)
        sys.exit(1)

    # set logging
    logger = logging.getLogger()
    logger.setLevel(config.loglevel)
    outlog = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(config.formatter)
    outlog.setFormatter(formatter)
    logger.addHandler(outlog)

    # start script
    msg='> Starting POOL script %s @ %s' % (__version__, str(datetime.datetime.now()))
    logger.info(msg)

    # subscribe to signals
    killsig = Event()
    signal.signal(signal.SIGINT, sighandler)
    signal.signal(signal.SIGTERM, sighandler)

    # load network
    network = Network(config.network)
    
    # load utility and client
    utility = Utility(network)
    client = utility.get_client()

    # connect to tbw script database
    sql = Sql()

    tags = {
       'dname': config.delegate,
       'proposal1': poolconfig.proposal1,
       'proposal2': poolconfig.proposal2,
       'proposal2_lang': poolconfig.proposal2_lang,
       'explorer': poolconfig.explorer,
       'coin': poolconfig.coin}

    #app.run(host=data.pool_ip, port=data.pool_port)
    server = Process(target=app.run, args=(poolconfig.pool_ip, poolconfig.pool_port))
    server.start()
