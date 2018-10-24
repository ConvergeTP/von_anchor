"""
Copyright 2017-2018 Government of Canada - Public Services and Procurement Canada - buyandsell.gc.ca

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
"""


import asyncio
import json
import logging

from os import listdir, makedirs, rename
from os.path import basename, expanduser, isdir, isfile, join
from shutil import rmtree

from indy import anoncreds, blob_storage, ledger
from indy.error import IndyError, ErrorCode
from von_anchor.anchor.origin import Origin
from von_anchor.cache import RevoCacheEntry, CRED_DEF_CACHE, REVO_CACHE
from von_anchor.codec import cred_attr_value
from von_anchor.error import (
    AbsentCredDef,
    AbsentSchema,
    AbsentRevReg,
    AbsentTails,
    BadIdentifier,
    BadLedgerTxn,
    BadRevocation,
    CorruptTails,
    CorruptWallet)
from von_anchor.nodepool import NodePool
from von_anchor.tails import Tails
from von_anchor.util import (
    cred_def_id,
    cred_def_id2seq_no,
    ok_cred_def_id,
    ok_rev_reg_id,
    ok_schema_id,
    rev_reg_id,
    rev_reg_id2cred_def_id,
    rev_reg_id2cred_def_id_tag,
    schema_key)
from von_anchor.wallet import Wallet

# import time
# from von_anchor.frill import Ink


LOGGER = logging.getLogger(__name__)


class Issuer(Origin):
    """
    Mixin for anchor acting in role of Issuer. An Issuer creates credential definitions and
    sends them to the ledger, issues credentials, and revokes credentials. Revocation support
    involves the management of tails files and revocation registries.

    For simplicity, the current design calls to make any issuer anchor an origin anchor.
    """

    def __init__(self, wallet: Wallet, pool: NodePool) -> None:
        """
        Initializer for Issuer anchor. Retain input parameters; do not open wallet nor tails writer.

        :param wallet: wallet for anchor use
        :param pool: pool for anchor use
        """

        LOGGER.debug('Issuer.__init__ >>> wallet: %s, pool: %s', wallet, pool)

        super().__init__(wallet, pool)
        self._dir_tails = join(expanduser('~'), '.indy_client', 'tails')
        self._dir_tails_hopper = join(self._dir_tails, '.hopper')
        makedirs(self._dir_tails_hopper, exist_ok=True)

        LOGGER.debug('Issuer.__init__ <<<')

    async def open(self) -> 'Issuer':
        """
        Explicit entry. Perform ancestor opening operations,
        then synchronize revocation registry to tails tree content.

        :return: current object
        """

        LOGGER.debug('Issuer.open >>>')

        await super().open()
        for path_rr_id in Tails.links(self._dir_tails, self.did):
            await self._sync_revoc_for_issue(basename(path_rr_id))

        LOGGER.debug('Issuer.open <<<')
        return self

    async def _create_rev_reg(self, rr_id: str, rr_size: int = None) -> None:
        """
        Create, in tails hopper directory, revocation registry artifacts and new tails file
        (with association to corresponding revocation registry identifier via symbolic link name)
        for input revocation registry identifier. Symbolic link presence signals completion.

        :param rr_id: revocation registry identifier
        :param rr_size: revocation registry size (defaults to 256)
        """

        LOGGER.debug('Issuer._create_rev_reg >>> rr_id: %s, rr_size: %s', rr_id, rr_size)

        # print(Ink.MAGENTA(f'\n\n start: rev reg {rr_id}'), end='', flush=True)
        # __mark = [time.time()] * 2

        if not ok_rev_reg_id(rr_id):
            LOGGER.debug('Issuer._create_rev_reg <!< Bad rev reg id %s', rr_id)
            raise BadIdentifier('Bad rev reg id {}'.format(rr_id))

        if len({t for t in asyncio.all_tasks()
            if t._coro.__qualname__ == 'Issuer._create_rev_reg'
                and not t.done()
                and t._coro.cr_frame
                and t._coro.cr_frame.f_locals.get('rr_id', None) == rr_id}) > 1:  # current task counts!
            LOGGER.warning(
                'Issuer %s abstaining from creating rev reg %s; already in progress',
                self.wallet.name,
                rr_id)
            return

        rr_size = rr_size or 256
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..0({__mark[1] - __mark[0]:.2f}) '), end='', flush=True)
        (cd_id, tag) = rev_reg_id2cred_def_id_tag(rr_id)
        dir_hopper_rr_id = join(self._dir_tails_hopper, rr_id)
        dir_hopper_target = join(dir_hopper_rr_id, cd_id)
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..1({__mark[1] - __mark[0]:.2f}) '), end='', flush=True)
        try:
            makedirs(dir_hopper_target, exist_ok=False)
            # __mark[:] = [__mark[1], time.time()]
            # print(Ink.MAGENTA(f'..2({__mark[1] - __mark[0]:.2f}) '), end='', flush=True)
        except FileExistsError:
            LOGGER.warning(
                'Issuer._create_rev_reg found dir %s existed but no such task not in progress: rebuilding rev reg %s',
                dir_hopper_target,
                rr_id)
            rmtree(dir_hopper_target)
            makedirs(dir_hopper_target, exist_ok=False)

        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..3({__mark[1] - __mark[0]:.2f}).. '), end='', flush=True)

        LOGGER.info('Creating revocation registry (capacity %s) for rev reg id %s', rr_size, rr_id)
        tails_writer_handle = await blob_storage.open_writer(
            'default',
            json.dumps({
                'base_dir': dir_hopper_target,
                'uri_pattern': ''
            }))

        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..4({__mark[1] - __mark[0]:.2f}).. '), end='', flush=True)
        apriori = Tails.unlinked(dir_hopper_target)
        await asyncio.sleep(0)  # issuer_create_and_store_revoc_reg is expensive: co-operate
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..5({__mark[1] - __mark[0]:.2f}).. '), end='', flush=True)
        (rr_id, rrd_json, rre_json) = await anoncreds.issuer_create_and_store_revoc_reg(
            self.wallet.handle,
            self.did,
            'CL_ACCUM',
            tag,
            cd_id,
            json.dumps({
                'max_cred_num': rr_size,
                'issuance_type': 'ISSUANCE_ON_DEMAND'
            }),
            tails_writer_handle)
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..6({__mark[1] - __mark[0]:.2f}).. '), end='', flush=True)
        await asyncio.sleep(0)  # issuer_create_and_store_revoc_reg is expensive: co-operate
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..7({__mark[1] - __mark[0]:.2f}).. '), end='', flush=True)
        delta = Tails.unlinked(dir_hopper_target) - apriori
        if len(delta) != 1:
            LOGGER.debug(
                'Issuer._create_rev_reg <!< Could not create tails file for rev reg id: %s', rr_id)
            raise CorruptTails('Could not create tails file for rev reg id {}'.format(rr_id))

        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..8({__mark[1] - __mark[0]:.2f}).. '), end='', flush=True)
        with open(join(dir_hopper_target, 'rrd.json'), 'w') as rrd_file:
            print(rrd_json, file=rrd_file)
        with open(join(dir_hopper_target, 'rre.json'), 'w') as rre_file:
            print(rre_json, file=rre_file)
        tails_hash = basename(delta.pop())
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..9({__mark[1] - __mark[0]:.2f}).. '), end='', flush=True)
        Tails.associate(dir_hopper_rr_id, rr_id, tails_hash)
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.MAGENTA(f'..ready: rev reg {rr_id}'), flush=True)

        LOGGER.debug('Issuer._create_rev_reg <<<')

    async def _send_rev_reg_def(self, rr_id: str) -> None:
        """
        Move tails file from hopper; deserialize revocation registry definition and initial entry;
        send to ledger and cache revocation registry definition.

        Operation serializes to subdirectory within tails hopper directory; symbolic
        link presence signals completion.

        Raise AbsentRevReg if revocation registry is not ready in hopper, or AbsentTails if
        tails file is not yet linked by its revocation registry identifier.

        :param rr_id: revocation registry identifier
        """

        LOGGER.debug('Issuer._send_rev_reg_def >>> rr_id: %s', rr_id)

        cd_id = rev_reg_id2cred_def_id(rr_id)
        dir_hopper_rr_id = join(self._dir_tails_hopper, rr_id)
        dir_hopper_target = join(dir_hopper_rr_id, cd_id)

        if not Tails.linked(dir_hopper_rr_id, rr_id):
            LOGGER.debug(
                'Issuer._send_rev_reg_def <!< Tails file for rev reg %s not ready in dir %s',
                rr_id,
                dir_hopper_target)
            raise AbsentRevReg('Tails file for rev reg {} not ready in dir {}'.format(rr_id, dir_hopper_target))

        file_rrd = join(dir_hopper_target, 'rrd.json')
        if not isfile(file_rrd):
            LOGGER.debug('Issuer._send_rev_reg_def <!< Rev reg def file %s not present', file_rrd)
            raise AbsentRevReg('Rev reg def file {} not present'.format(file_rrd))
        with open(file_rrd, 'r') as rrd:
            rrd_json = rrd.read()

        file_rre = join(dir_hopper_target, 'rre.json')
        if not isfile(file_rre):
            LOGGER.debug('Issuer._send_rev_reg_def <!< Rev reg entry file %s not present', file_rre)
            raise AbsentRevReg('Rev reg entry file {} not present'.format(file_rre))
        with open(file_rre, 'r') as rre:
            rre_json = rre.read()

        file_tails = Tails.linked(dir_hopper_rr_id, rr_id)
        if not file_tails:
            LOGGER.debug('Issuer._send_rev_reg_def <!< Tails link %s not present in dir %s', rr_id, dir_hopper_target)
            raise AbsentTails('Tails link {} not present in dir {}'.format(rr_id, dir_hopper_target))

        dir_target = join(self._dir_tails, cd_id)
        makedirs(dir_target, exist_ok=True)
        rename(file_tails, join(dir_target, basename(file_tails)))

        with REVO_CACHE.lock:
            rrd_req_json = await ledger.build_revoc_reg_def_request(self.did, rrd_json)
            await self._sign_submit(rrd_req_json)
            await self._get_rev_reg_def(rr_id)  # add to cache en passant

        rre_req_json = await ledger.build_revoc_reg_entry_request(self.did, rr_id, 'CL_ACCUM', rre_json)
        await self._sign_submit(rre_req_json)

        Tails.associate(self._dir_tails, rr_id, basename(file_tails))
        rmtree(dir_hopper_rr_id)

        LOGGER.debug('Issuer._send_rev_reg_def <<<')


    async def _set_rev_reg(self, rr_id: str, rr_size: int) -> None:
        """
        Move precomputed revocation registry data from hopper into place within tails directory.

        :param rr_id: revocation registry identifier
        :param rr_size: revocation registry size, in case creation required
        """

        LOGGER.debug('Issuer._set_rev_reg >>> rr_id: %s, rr_size: %s', rr_id, rr_size)

        # print(Ink.CYAN(f'\n\nset 0: rev reg {rr_id}'), flush=True)
        # __mark = [time.time()] * 2
        dir_hopper_rr_id = join(self._dir_tails_hopper, rr_id)
        if not (Tails.linked(dir_hopper_rr_id, rr_id) or any({t for t in asyncio.all_tasks()
            if t._coro.__qualname__ == 'Issuer._create_rev_reg'
                and not t.done()
                and t._coro.cr_frame
                and t._coro.cr_frame.f_locals.get('rr_id', None) == rr_id})):
            LOGGER.info(
                'Issuer %s waiting on rev reg %s, neither ready nor pending: starting pre-computation',
                self.wallet.name,
                rr_id)
            asyncio.create_task(self._create_rev_reg(rr_id, rr_size))  # nothing happening, kick off pre-computation

        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.CYAN(f'set 1({__mark[1] - __mark[0]:.2f}: rev reg {rr_id}'), flush=True)
        while Tails.linked(dir_hopper_rr_id, rr_id) is None:
            await asyncio.sleep(1)
        await self._send_rev_reg_def(rr_id)

        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.CYAN(f'set 2({__mark[1] - __mark[0]:.2f}: rev reg {rr_id}'), flush=True)
        cd_id = rev_reg_id2cred_def_id(rr_id)
        (next_tag, rr_size_suggested) = Tails.next_tag(self._dir_tails, cd_id)
        asyncio.create_task(self._create_rev_reg(
            rev_reg_id(cd_id, next_tag),
            rr_size_suggested))  # pre-compute next rev reg
        # __mark[:] = [__mark[1], time.time()]
        # print(Ink.CYAN(f'set 3({__mark[1] - __mark[0]:.2f}: rev reg {rr_id}'), flush=True)
        LOGGER.debug('Issuer._set_rev_reg <<<')

    async def _sync_revoc_for_issue(self, rr_id: str, rr_size: int = None) -> None:
        """
        Create revocation registry if need be for input revocation registry identifier;
        open and cache tails file reader.

        :param rr_id: revocation registry identifier
        :param rr_size: if new revocation registry necessary, its size (default as per _create_rev_reg())
        """

        LOGGER.debug('Issuer._sync_revoc_for_issue >>> rr_id: %s, rr_size: %s', rr_id, rr_size)

        if not ok_rev_reg_id(rr_id):
            LOGGER.debug('Issuer._sync_revoc_for_issue <!< Bad rev reg id %s', rr_id)
            raise BadIdentifier('Bad rev reg id {}'.format(rr_id))

        (cd_id, tag) = rev_reg_id2cred_def_id_tag(rr_id)

        try:
            await self.get_cred_def(cd_id)
        except AbsentCredDef:
            LOGGER.debug(
                'Issuer._sync_revoc_for_issue <!< tails tree %s may be for another ledger; no cred def found on %s',
                self._dir_tails,
                cd_id)
            raise AbsentCredDef('Tails tree {} may be for another ledger; no cred def found on {}'.format(
                self._dir_tails,
                cd_id))

        with REVO_CACHE.lock:
            revo_cache_entry = REVO_CACHE.get(rr_id, None)
            tails = None if revo_cache_entry is None else revo_cache_entry.tails
            if tails is None:  #  it's a new revocation registry, or not yet set in cache
                try:
                    tails = await Tails(self._dir_tails, cd_id, tag).open()
                except AbsentTails:
                    await self._set_rev_reg(rr_id, rr_size)
                    tails = await Tails(self._dir_tails, cd_id, tag).open()  # symlink should exist now

                if revo_cache_entry is None:
                    REVO_CACHE[rr_id] = RevoCacheEntry(None, tails)
                else:
                    REVO_CACHE[rr_id].tails = tails

        LOGGER.debug('Issuer._sync_revoc_for_issue <<<')

    def path_tails(self, rr_id: str) -> str:
        """
        Return path to tails file for input revocation registry identifier.

        :param rr_id: revocation registry identifier of interest
        :return: path to tails file for input revocation registry identifier
        """

        LOGGER.debug('Issuer.path_tails >>>')

        if not ok_rev_reg_id(rr_id):
            LOGGER.debug('Issuer.path_tails <!< Bad rev reg id %s', rr_id)
            raise BadIdentifier('Bad rev reg id {}'.format(rr_id))

        rv = Tails.linked(self._dir_tails, rr_id)
        LOGGER.debug('Issuer.path_tails <<< %s', rv)
        return rv

    async def send_cred_def(self, s_id: str, revocation: bool = True, rr_size: int = None) -> str:
        """
        Create a credential definition as Issuer, store it in its wallet, and send it to the ledger.

        Raise CorruptWallet for wallet not pertaining to current ledger, BadLedgerTxn on failure
        to send credential definition to ledger if need be, or IndyError for any other failure
        to create and store credential definition in wallet.

        :param s_id: schema identifier
        :param revocation: whether to support revocation for cred def
        :param rr_size: size of initial revocation registry (default as per _create_rev_reg()), if revocation supported
        :return: json credential definition as it appears on ledger
        """

        LOGGER.debug('Issuer.send_cred_def >>> s_id: %s, revocation: %s, rr_size: %s', s_id, revocation, rr_size)

        if not ok_schema_id(s_id):
            LOGGER.debug('Issuer.send_cred_def <!< Bad schema id %s', s_id)
            raise BadIdentifier('Bad schema id {}'.format(s_id))

        rv_json = json.dumps({})
        schema_json = await self.get_schema(schema_key(s_id))
        schema = json.loads(schema_json)

        cd_id = cred_def_id(self.did, schema['seqNo'], self.pool.protocol)
        private_key_ok = True
        with CRED_DEF_CACHE.lock:
            try:
                rv_json = await self.get_cred_def(cd_id)
                LOGGER.info(
                    'Cred def on schema %s version %s already exists on ledger; Issuer %s not sending another',
                    schema['name'],
                    schema['version'],
                    self.wallet.name)
            except AbsentCredDef:
                pass  # OK - about to create, store, and send it

            try:
                (_, cred_def_json) = await anoncreds.issuer_create_and_store_credential_def(
                    self.wallet.handle,
                    self.did,  # issuer DID
                    schema_json,
                    self.pool.protocol.cd_id_tag(False),  # expect only one cred def per schema and issuer
                    'CL',
                    json.dumps({'support_revocation': revocation}))
                if json.loads(rv_json):
                    private_key_ok = False
                    LOGGER.warning(
                        'New cred def on %s in wallet shadows existing one on ledger: private key not usable', cd_id)
                        # carry on though, this anchor may have other roles so public key may be good enough
            except IndyError as x_indy:
                if x_indy.error_code == ErrorCode.AnoncredsCredDefAlreadyExistsError:
                    if json.loads(rv_json):
                        LOGGER.info(
                            'Issuer wallet %s reusing existing cred def on schema %s version %s',
                            self.wallet.name,
                            schema['name'],
                            schema['version'])
                    else:
                        LOGGER.debug('Issuer.send_cred_def <!< corrupt wallet %s', self.wallet.name)
                        raise CorruptWallet(
                            'Corrupt Issuer wallet {} has cred def on schema {} version {} not on ledger'.format(
                                self.wallet.name,
                                schema['name'],
                                schema['version']))
                else:
                    LOGGER.debug(
                        'Issuer.send_cred_def <!< cannot store cred def in wallet %s: indy error code %s',
                        self.wallet.name,
                        x_indy.error_code)
                    raise

            if not json.loads(rv_json):  # checking the ledger returned no cred def: send it
                req_json = await ledger.build_cred_def_request(self.did, cred_def_json)
                await self._sign_submit(req_json)

                for _ in range(16):  # reasonable timeout
                    try:
                        rv_json = await self.get_cred_def(cd_id)  # adds to cache
                        break
                    except AbsentCredDef:
                        await asyncio.sleep(1)
                        LOGGER.info('Sent cred def %s to ledger, waiting 1s for its appearance', cd_id)

                if not rv_json:
                    LOGGER.debug('Issuer.send_cred_def <!< timed out waiting on sent cred_def %s', cd_id)
                    raise BadLedgerTxn('Timed out waiting on sent cred_def {}'.format(cd_id))

                if revocation:
                    await self._sync_revoc_for_issue(rev_reg_id(cd_id, '0'), rr_size)  # create new rev reg for tag '0'

                    (next_tag, rr_size_suggested) = Tails.next_tag(self._dir_tails, cd_id)
                    asyncio.create_task(self._create_rev_reg(
                        rev_reg_id(cd_id, next_tag),
                        rr_size_suggested))  # pre-compute next rev reg

        if revocation and private_key_ok:
            for tag in [str(t) for t in range(int(Tails.next_tag(self._dir_tails, cd_id)[0]))]:  # '0' to str(next-1)
                await self._sync_revoc_for_issue(rev_reg_id(cd_id, tag), rr_size if tag == '0' else None)

        makedirs(join(self._dir_tails, cd_id), exist_ok=True)  # make sure dir exists for box id collection, revo or not

        LOGGER.debug('Issuer.send_cred_def <<< %s', rv_json)
        return rv_json

    async def create_cred_offer(self, schema_seq_no: int) -> str:
        """
        Create credential offer as Issuer for given schema.

        Raise CorruptWallet if the wallet has no private key for the corresponding credential definition.

        :param schema_seq_no: schema sequence number
        :return: credential offer json for use in storing credentials at HolderProver.
        """

        LOGGER.debug('Issuer.create_cred_offer >>> schema_seq_no: %s', schema_seq_no)

        rv = None
        cd_id = cred_def_id(self.did, schema_seq_no, self.pool.protocol)
        try:
            rv = await anoncreds.issuer_create_credential_offer(self.wallet.handle, cd_id)
        except IndyError as x_indy:
            if x_indy.error_code == ErrorCode.WalletNotFoundError:
                LOGGER.debug(
                    'Issuer.create_cred_offer <!< did not issue cred definition from wallet %s',
                    self.wallet.name)
                raise CorruptWallet(
                    'Cannot create cred offer: did not issue cred definition from wallet {}'.format(self.wallet.name))
            else:
                LOGGER.debug(
                    'Issuer.create_cred_offer <!<  cannot create cred offer, indy error code %s',
                    x_indy.error_code)
                raise

        LOGGER.debug('Issuer.create_cred_offer <<< %s', rv)
        return rv

    async def create_cred(
            self,
            cred_offer_json,
            cred_req_json: str,
            cred_attrs: dict,
            rr_size: int = None) -> (str, str, int):
        """
        Create credential as Issuer out of credential request and dict of key:value (raw, unencoded)
        entries for attributes.

        Return credential json, and if cred def supports revocation, credential revocation identifier
        and revocation registry delta ledger timestamp (epoch seconds).

        If the credential definition supports revocation, and the current revocation registry is full,
        the processing creates a new revocation registry en passant. Depending on the revocation
        registry size (by default starting at 256 and doubling iteratively through 16384), this
        operation may delay credential creation by several seconds.

        :param cred_offer_json: credential offer json as created by Issuer
        :param cred_req_json: credential request json as created by HolderProver
        :param cred_attrs: dict mapping each attribute to its raw value (the operation encodes it); e.g.,

        ::

            {
                'favourite_drink': 'martini',
                'height': 180,
                'last_visit_date': '2017-12-31',
                'weaknesses': None
            }

        :param rr_size: size of new revocation registry (default as per _create_rev_reg()) if necessary
        :return: newly issued credential json; credential revocation identifier (if cred def supports
            revocation, None otherwise), and ledger timestamp (if cred def supports revocation, None otherwise)
        """

        LOGGER.debug(
            'Issuer.create_cred >>> cred_offer_json: %s, cred_req_json: %s, cred_attrs: %s, rr_size: %s',
            cred_offer_json,
            cred_req_json,
            cred_attrs,
            rr_size)

        cd_id = json.loads(cred_offer_json)['cred_def_id']
        if not ok_cred_def_id(cd_id):
            LOGGER.debug('Issuer.create_cred <!< Bad cred def id %s', cd_id)
            raise BadIdentifier('Bad cred def id {}'.format(cd_id))

        cred_def = json.loads(await self.get_cred_def(cd_id))  # ensure cred def is in cache

        if 'revocation' in cred_def['value']:
            with REVO_CACHE.lock:
                rr_id = Tails.current_rev_reg_id(self._dir_tails, cd_id)
                tails = REVO_CACHE[rr_id].tails
                assert tails  # at (re)start, at cred def, Issuer sync_revoc_for_issue() sets this index in revo cache

                try:
                    (cred_json, cred_revoc_id, rr_delta_json) = await anoncreds.issuer_create_credential(
                        self.wallet.handle,
                        cred_offer_json,
                        cred_req_json,
                        json.dumps({k: cred_attr_value(cred_attrs[k]) for k in cred_attrs}),
                        rr_id,
                        tails.reader_handle)
                    # do not create rr delta frame and append to cached delta frames list: timestamp could lag or skew
                    rre_req_json = await ledger.build_revoc_reg_entry_request(
                        self.did,
                        rr_id,
                        'CL_ACCUM',
                        rr_delta_json)
                    await self._sign_submit(rre_req_json)
                    assert rr_id == tails.rr_id
                    resp_json = await self._sign_submit(rre_req_json)
                    resp = json.loads(resp_json)
                    rv = (cred_json, cred_revoc_id, self.pool.protocol.txn2epoch(resp))

                except IndyError as x_indy:
                    if x_indy.error_code == ErrorCode.AnoncredsRevocationRegistryFullError:
                        (tag, rr_size_suggested) = Tails.next_tag(self._dir_tails, cd_id)
                        rr_id = rev_reg_id(cd_id, tag)
                        await self._set_rev_reg(rr_id, rr_size or rr_size_suggested)

                        REVO_CACHE[rr_id].tails = await Tails(self._dir_tails, cd_id).open()  # symlink should exist now
                        return await self.create_cred(cred_offer_json, cred_req_json, cred_attrs)

                    LOGGER.debug('Issuer.create_cred <!<  cannot create cred, indy error code %s', x_indy.error_code)
                    raise
        else:
            try:
                (cred_json, _, _) = await anoncreds.issuer_create_credential(
                    self.wallet.handle,
                    cred_offer_json,
                    cred_req_json,
                    json.dumps({k: cred_attr_value(cred_attrs[k]) for k in cred_attrs}),
                    None,
                    None)
                rv = (cred_json, _, _)
            except IndyError as x_indy:
                LOGGER.debug('Issuer.create_cred <!<  cannot create cred, indy error code %s', x_indy.error_code)
                raise

        LOGGER.debug('Issuer.create_cred <<< %s', rv)
        return rv

    async def revoke_cred(self, rr_id: str, cr_id) -> int:
        """
        Revoke credential that input revocation registry identifier and
        credential revocation identifier specify.

        Return (epoch seconds) time of revocation.

        Raise AbsentTails if no tails file is available for input
        revocation registry identifier. Raise BadRevocation if issuer cannot
        revoke specified credential for any other reason (e.g., did not issue it,
        already revoked it).

        :param rr_id: revocation registry identifier
        :param cr_id: credential revocation identifier
        :return: time of revocation, in epoch seconds
        """

        LOGGER.debug('Issuer.revoke_cred >>> rr_id: %s, cr_id: %s', rr_id, cr_id)

        if not ok_rev_reg_id(rr_id):
            LOGGER.debug('Issuer.revoke_cred <!< Bad rev reg id %s', rr_id)
            raise BadIdentifier('Bad rev reg id {}'.format(rr_id))

        tails_reader_handle = (await Tails(
            self._dir_tails,
            *rev_reg_id2cred_def_id_tag(rr_id)).open()).reader_handle
        try:
            rrd_json = await anoncreds.issuer_revoke_credential(
                self.wallet.handle,
                tails_reader_handle,
                rr_id,
                cr_id)
        except IndyError as x_indy:
            LOGGER.debug(
                'Issuer.revoke_cred <!< Could not revoke revoc reg id %s, cred rev id %s: indy error code %s',
                rr_id,
                cr_id,
                x_indy.error_code)
            raise BadRevocation(
                'Could not revoke revoc reg id {}, cred rev id {}: indy error code {}'.format(
                    rr_id,
                    cr_id,
                    x_indy.error_code))

        rre_req_json = await ledger.build_revoc_reg_entry_request(self.did, rr_id, 'CL_ACCUM', rrd_json)
        resp_json = await self._sign_submit(rre_req_json)
        resp = json.loads(resp_json)

        rv = self.pool.protocol.txn2epoch(resp)
        LOGGER.debug('Issuer.revoke_cred <<< %s', rv)
        return rv

    async def get_box_ids_issued(self) -> str:
        """
        Return json object on lists of all unique box identifiers (schema identifiers,
        credential definition identifiers, and revocation registry identifiers) for
        all credential definitions and credentials issued; e.g.,

        ::

            {
                "schema_id": [
                    "R17v42T4pk...:2:tombstone:1.2",
                    ...
                ],
                "cred_def_id": [
                    "R17v42T4pk...:3:CL:19:tag",
                    ...
                ]
                "rev_reg_id": [
                    "R17v42T4pk...:4:R17v42T4pk...:3:CL:19:tag:CL_ACCUM:0",
                    "R17v42T4pk...:4:R17v42T4pk...:3:CL:19:tag:CL_ACCUM:1",
                    ...
                ]
            }

        An issuer must issue a credential definition to include its schema identifier
        in the returned values; the schema identifier in isolation belongs properly
        to an Origin, not necessarily to an Issuer.

        The operation may be useful for a Verifier anchor going off-line to seed its
        cache before doing so.

        :return: tuple of sets for schema ids, cred def ids, rev reg ids
        """

        LOGGER.debug('Issuer.get_box_ids_issued >>>')

        cd_ids = [d for d in listdir(self._dir_tails)
            if isdir(join(self._dir_tails, d)) and ok_cred_def_id(d) and d.startswith('{}:3:'.format(self.did))]
        s_ids = []
        for cd_id in cd_ids:
            try:
                s_ids.append(json.loads(await self.get_schema(cred_def_id2seq_no(cd_id)))['id'])
            except AbsentSchema:
                LOGGER.error(
                    'Issuer %s has issued cred def %s but no corresponding schema on ledger',
                    self.wallet.name,
                    cd_id)
        rr_ids = [basename(link) for link in Tails.links(self._dir_tails, self.did)]

        rv = json.dumps({
            'schema_id': s_ids,
            'cred_def_id': cd_ids,
            'rev_reg_id': rr_ids
        })
        LOGGER.debug('Issuer.get_box_ids_issued <<< %s', rv)
        return rv
