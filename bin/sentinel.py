#!/usr/bin/env python
import sys
import os
sys.path.append(os.path.normpath(os.path.join(os.path.dirname(__file__), '../lib')))
import init
import config
import misc
from bytzd import BytzDaemon
from models import Superblock, Proposal, GovernanceObject
from models import VoteSignals, VoteOutcomes, Transient
import socket
from misc import printdbg
import time
from bitcoinrpc.authproxy import JSONRPCException
import signal
import atexit
import random
from scheduler import Scheduler
import argparse


# sync bytzd gobject list with our local relational DB backend
def perform_bytzd_object_sync(bytzd):
    GovernanceObject.sync(bytzd)


def prune_expired_proposals(bytzd):
    # vote delete for old proposals
    for proposal in Proposal.expired(bytzd.superblockcycle()):
        proposal.vote(bytzd, VoteSignals.delete, VoteOutcomes.yes)


def attempt_superblock_creation(bytzd):
    import bytzlib

    if not bytzd.is_masternode():
        print("We are not a Masternode... can't submit superblocks!")
        return

    # query votes for this specific ebh... if we have voted for this specific
    # ebh, then it's voted on. since we track votes this is all done using joins
    # against the votes table
    #
    # has this masternode voted on *any* superblocks at the given event_block_height?
    # have we voted FUNDING=YES for a superblock for this specific event_block_height?

    event_block_height = bytzd.next_superblock_height()

    if Superblock.is_voted_funding(event_block_height):
        # printdbg("ALREADY VOTED! 'til next time!")

        # vote down any new SBs because we've already chosen a winner
        for sb in Superblock.at_height(event_block_height):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(bytzd, VoteSignals.funding, VoteOutcomes.no)

        # now return, we're done
        return

    if not bytzd.is_govobj_maturity_phase():
        printdbg("Not in maturity phase yet -- will not attempt Superblock")
        return

    proposals = Proposal.approved_and_ranked(proposal_quorum=bytzd.governance_quorum(), next_superblock_max_budget=bytzd.next_superblock_max_budget())
    budget_max = bytzd.get_superblock_budget_allocation(event_block_height)
    sb_epoch_time = bytzd.block_height_to_epoch(event_block_height)

    sb = bytzlib.create_superblock(proposals, event_block_height, budget_max, sb_epoch_time)
    if not sb:
        printdbg("No superblock created, sorry. Returning.")
        return

    # find the deterministic SB w/highest object_hash in the DB
    dbrec = Superblock.find_highest_deterministic(sb.hex_hash())
    if dbrec:
        dbrec.vote(bytzd, VoteSignals.funding, VoteOutcomes.yes)

        # any other blocks which match the sb_hash are duplicates, delete them
        for sb in Superblock.select().where(Superblock.sb_hash == sb.hex_hash()):
            if not sb.voted_on(signal=VoteSignals.funding):
                sb.vote(bytzd, VoteSignals.delete, VoteOutcomes.yes)

        printdbg("VOTED FUNDING FOR SB! We're done here 'til next superblock cycle.")
        return
    else:
        printdbg("The correct superblock wasn't found on the network...")

    # if we are the elected masternode...
    if (bytzd.we_are_the_winner()):
        printdbg("we are the winner! Submit SB to network")
        sb.submit(bytzd)


def check_object_validity(bytzd):
    # vote (in)valid objects
    for gov_class in [Proposal, Superblock]:
        for obj in gov_class.select():
            obj.vote_validity(bytzd)


def is_bytzd_port_open(bytzd):
    # test socket open before beginning, display instructive message to MN
    # operators if it's not
    port_open = False
    try:
        info = bytzd.rpc_command('getgovernanceinfo')
        port_open = True
    except (socket.error, JSONRPCException) as e:
        print("%s" % e)

    return port_open


def main():
    bytzd = BytzDaemon.from_bytz_conf(config.bytz_conf)
    options = process_args()

    # print version and return if "--version" is an argument
    if options.version:
        print("Bytz Sentinel v%s" % config.sentinel_version)
        return

    # check bytzd connectivity
    if not is_bytzd_port_open(bytzd):
        print("Cannot connect to bytzd. Please ensure bytzd is running and the JSONRPC port is open to Sentinel.")
        return

    # check bytzd sync
    if not bytzd.is_synced():
        print("bytzd not synced with network! Awaiting full sync before running Sentinel.")
        return

    # ensure valid masternode
    if not bytzd.is_masternode():
        print("Invalid Masternode Status, cannot continue.")
        return

    # register a handler if SENTINEL_DEBUG is set
    if os.environ.get('SENTINEL_DEBUG', None):
        import logging
        logger = logging.getLogger('peewee')
        logger.setLevel(logging.DEBUG)
        logger.addHandler(logging.StreamHandler())

    if options.bypass:
        # bypassing scheduler, remove the scheduled event
        printdbg("--bypass-schedule option used, clearing schedule")
        Scheduler.clear_schedule()

    if not Scheduler.is_run_time():
        printdbg("Not yet time for an object sync/vote, moving on.")
        return

    if not options.bypass:
        # delay to account for cron minute sync
        Scheduler.delay()

    # running now, so remove the scheduled event
    Scheduler.clear_schedule()

    # ========================================================================
    # general flow:
    # ========================================================================
    #
    # load "gobject list" rpc command data, sync objects into internal database
    perform_bytzd_object_sync(bytzd)

    # auto vote network objects as valid/invalid
    # check_object_validity(bytzd)

    # vote to delete expired proposals
    prune_expired_proposals(bytzd)

    # create a Superblock if necessary
    attempt_superblock_creation(bytzd)

    # schedule the next run
    Scheduler.schedule_next_run()


def signal_handler(signum, frame):
    print("Got a signal [%d], cleaning up..." % (signum))
    Transient.delete('SENTINEL_RUNNING')
    sys.exit(1)


def cleanup():
    Transient.delete(mutex_key)


def process_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-b', '--bypass-scheduler',
                        action='store_true',
                        help='Bypass scheduler and sync/vote immediately',
                        dest='bypass')
    parser.add_argument('-v', '--version',
                        action='store_true',
                        help='Print the version (Bytz Sentinel vX.X.X) and exit')

    args = parser.parse_args()

    return args


if __name__ == '__main__':
    atexit.register(cleanup)
    signal.signal(signal.SIGINT, signal_handler)

    # ensure another instance of Sentinel is not currently running
    mutex_key = 'SENTINEL_RUNNING'
    # assume that all processes expire after 'timeout_seconds' seconds
    timeout_seconds = 90

    is_running = Transient.get(mutex_key)
    if is_running:
        printdbg("An instance of Sentinel is already running -- aborting.")
        sys.exit(1)
    else:
        Transient.set(mutex_key, misc.now(), timeout_seconds)

    # locked to this instance -- perform main logic here
    main()

    Transient.delete(mutex_key)
