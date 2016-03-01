#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
    Blockstore
    ~~~~~
    copyright: (c) 2014-2015 by Halfmoon Labs, Inc.
    copyright: (c) 2016 by Blockstack.org

    This file is part of Blockstore

    Blockstore is free software: you can redistribute it and/or modify
    it under the terms of the GNU General Public License as published by
    the Free Software Foundation, either version 3 of the License, or
    (at your option) any later version.

    Blockstore is distributed in the hope that it will be useful,
    but WITHOUT ANY WARRANTY; without even the implied warranty of
    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
    GNU General Public License for more details.
    You should have received a copy of the GNU General Public License
    along with Blockstore. If not, see <http://www.gnu.org/licenses/>.
"""

import sqlite3

import json
import traceback
import binascii
import hashlib
import math
import keychain
import pybitcoin
import os
import sys
import copy
import shutil

from collections import defaultdict

# hack around absolute paths
curr_dir = os.path.abspath( os.path.join( os.path.dirname(__file__), ".." ) )
sys.path.insert( 0, curr_dir )

from ..config import * 
from ..operations import *
from ..hashing import *
from ..scripts import *
from ..b40 import *

import virtualchain

if not globals().has_key('log'):
    log = virtualchain.session.log

BLOCKSTORE_DB_SCRIPT = ""

BLOCKSTORE_DB_SCRIPT += """
-- NOTE: history_id is a fully-qualified name or namespace ID.
-- NOTE: history_data is a JSON-serialized dict of changed fields.
CREATE TABLE history( txid TEXT NOT NULL,
                      history_id STRING NOT NULL,
                      block_id INT NOT NULL,
                      vtxindex INT NOT NULL,
                      op TEXT NOT NULL,
                      history_data TEXT NOT NULL,
                      PRIMARY KEY(txid,history_id,block_id,vtxindex) );
"""

BLOCKSTORE_DB_SCRIPT += """
CREATE INDEX history_block_id_index ON history( history_id, block_id );
CREATE INDEX history_id_index ON history( history_id );
"""

BLOCKSTORE_DB_SCRIPT += """
-- NOTE: this table only grows.
-- The only time rows can be taken out is when a name or
-- namespace successfully matches it.
CREATE TABLE preorders( preorder_hash TEXT PRIMARY KEY UNIQUE NOT NULL,
                        consensus_hash TEXT NOT NULL,
                        sender TEXT NOT NULL,
                        sender_pubkey TEXT,
                        address TEXT,
                        block_number INT NOT NULL,
                        op TEXT NOT NULL,
                        op_fee INT NOT NULL,
                        txid TEXT NOT NULL,
                        vtxindex INT);
"""

BLOCKSTORE_DB_SCRIPT += """
-- NOTE: this table includes revealed namespaces
-- NOTE: 'buckets' is a JSON-serialized array of integers
CREATE TABLE namespaces( namespace_id STRING NOT NULL,
                         preorder_hash TEXT NOT NULL,
                         version INT,
                         sender TEXT NOT NULL,
                         sender_pubkey TEXT,
                         address TEXT,
                         recipient TEXT NOT NULL,
                         recipient_address TEXT,
                         block_number INT NOT NULL,
                         reveal_block INT NOT NULL,
                         op TEXT NOT NULL,
                         op_fee INT NOT NULL,
                         txid TEXT NOT NULL NOT NULL,
                         vtxindex INT NOT NULL,
                         lifetime INT NOT NULL,
                         coeff INT NOT NULL,
                         base INT NOT NULL,
                         buckets TEXT NOT NULL,
                         nonalpha_discount INT NOT NULL, 
                         no_vowel_discount INT NOT NULL,
                         ready_block INT NOT NULL,

                         -- primary key includes block number, so an expired revealed namespace can be re-revealed
                         PRIMARY KEY(namespace_id,block_number)
                         );
"""

BLOCKSTORE_DB_SCRIPT += """
CREATE TABLE name_records( name STRING NOT NULL,
                           preorder_hash TEXT NOT NULL,
                           name_hash128 TEXT NOT NULL,
                           namespace_id STRING NOT NULL,
                           namespace_block_number INT NOT NULL,
                           value_hash TEXT,
                           sender TEXT NOT NULL,
                           sender_pubkey TEXT,
                           address TEXT,
                           block_number INT NOT NULL,
                           preorder_block_number INT NOT NULL,
                           first_registered INT NOT NULL,
                           last_renewed INT NOT NULL,
                           revoked INT NOT NULL,
                           op TEXT NOT NULL,
                           txid TEXT NOT NULL,
                           vtxindex INT NOT NULL,
                           op_fee INT NOT NULL,
                           importer TEXT,
                           importer_address TEXT,
                           consensus_hash TEXT,

                           -- primary key includes block number, so an expired name can be re-registered 
                           PRIMARY KEY(name,block_number),

                           -- namespace must exist
                           FOREIGN KEY(namespace_id,namespace_block_number) REFERENCES namespaces(namespace_id,block_number)
                           );
"""

BLOCKSTORE_DB_SCRIPT += """
CREATE INDEX hash_names_index ON name_records( name_hash128, name );
"""

BLOCKSTORE_DB_SCRIPT += """
-- turn on foreign key constraints 
PRAGMA foreign_keys = ON;
"""


def namedb_create( path ):
    """
    Create a sqlite3 db at the given path.
    Create all the tables and indexes we need.
    """

    global BLOCKSTORE_DB_SCRIPT

    if os.path.exists( path ):
        raise Exception("Database '%s' already exists" % path)

    lines = [l + ";" for l in BLOCKSTORE_DB_SCRIPT.split(";")]
    con = sqlite3.connect( path, isolation_level=None )

    for line in lines:
        con.execute(line)

    con.row_factory = namedb_row_factory
    return con


def namedb_open( path ):
    """
    Open a connection to our database 
    """
    con = sqlite3.connect( path, isolation_level=None )
    con.row_factory = namedb_row_factory
    return con


def namedb_row_factory( cursor, row ):
    """
    Row factor to enforce some additional types:
    * force 'revoked' to be a bool
    """
    d = {}
    for idx, col in enumerate( cursor.description ):
        if col[0] == 'revoked':
            if row[idx] == 0:
                d[col[0]] = False
            elif row[idx] == 1:
                d[col[0]] = True
            else:
                raise Exception("Invalid value for 'revoked': %s" % row[idx])

        else:
            d[col[0]] = row[idx]

    return d


def namedb_assert_fields_match( cur, record, table_name, record_matches_columns=True, columns_match_record=True ):
    """
    Ensure that the fields of a given record match
    the columns of the given table.
    * if record_match_columns, then the keys in record must match all columns.
    * if columns_match_record, then the columns must match the keys in the record.

    Return True if so.
    Raise an exception if not.
    """
    
    rec_missing = []
    rec_extra = []
    
    # sanity check: all fields must be defined
    name_fields_rows = cur.execute("PRAGMA table_info(%s);" % table_name)
    name_fields = []
    for row in name_fields_rows:
        name_fields.append( row['name'] )

    if columns_match_record:
        # make sure each column has a record field
        for f in name_fields:
            if f not in record.keys():
                rec_missing.append( f )

    if record_matches_columns:
        # make sure each record field has a column
        for k in record.keys():
            if k not in name_fields:
                rec_extra.append( k )

    if len(rec_missing) != 0 or len(rec_extra) != 0:
        raise Exception("Invalid record: missing = %s, extra = %s" % 
                        (",".join(rec_missing), ",".join(rec_extra)))

    return True


def namedb_insert_prepare( cur, record, table_name ):
    """
    Prepare to insert a record, but make sure
    that all of the column names have values first!

    Return an INSERT INTO statement on success.
    Raise an exception if not.
    """

    namedb_assert_fields_match( cur, record, table_name )
     
    columns = record.keys()
    columns.sort()
    values = [record[c] for c in columns]
    values = tuple(values)

    field_placeholders = ",".join( ["?"] * len(columns) )

    query = "INSERT INTO %s (%s) VALUES (%s);" % (table_name, ",".join(columns), field_placeholders)
    return (query, values)


def namedb_update_prepare( cur, primary_key, input_record, table_name, must_equal=[], only_if={} ):
    """
    Prepare to update a record, but make sure that the fields in input_record
    correspond to acual columns.
    Also, enforce any fields that must be equal to the fields
    in the given record (must_equal), and require that certian fields in record
    have certain values first (only_if)

    Return an UPDATE ... SET ... WHERE statement on success.
    Raise an exception if not.

    DO NOT CALL THIS METHOD DIRECTLY
    """

    record = copy.deepcopy( input_record )
    must_equal_dict = dict( [(c, None) for c in must_equal] )

    # extract primary key
    # sanity check: primary key cannot be mutated
    primary_key_value = record.get(primary_key, None)
    assert primary_key_value is not None, "BUG: no primary key value given in record"
    assert primary_key in must_equal, "BUG: primary key set to change"
    assert len(must_equal) > 0, "BUG: no identifying information for this record"

    # find set of columns that will change 
    update_columns = []
    for k in input_record.keys():
        if k not in must_equal:
            update_columns.append( k )

    # record keys correspond to real columns
    namedb_assert_fields_match( cur, record, table_name, columns_match_record=False )

    # must_equal keys correspond to real columns
    namedb_assert_fields_match( cur, must_equal_dict, table_name, columns_match_record=False )

    # only_if keys correspond to real columns 
    namedb_assert_fields_match( cur, only_if, table_name, columns_match_record=False )

    # only_if does not overlap with must_equal
    assert len( set(must_equal).intersection(only_if.keys()) ) == 0, "BUG: only_if and must_equal overlap"

    update_values = [record[c] for c in update_columns]
    update_values = tuple(update_values)
    update_set = [("%s = ?" % c) for c in update_columns]

    where_set = []
    where_values = []
    for c in must_equal:
        if record[c] is None:
            where_set.append( "%s IS NULL" % c )
        else:
            where_set.append( "%s = ?" % c)
            where_values.append( record[c] )


    for c in only_if.keys():
        if only_if[c] is None:
            where_set.append( "%s IS NULL" % c)
        else:
            where_set.append( "%s = ?" % c)
            where_set.values.append( only_if[c] )

    where_values = tuple(where_values)

    query = "UPDATE %s SET %s WHERE %s" % (table_name, ", ".join(update_set), " AND ".join(where_set))
    return (query, update_values + where_values)


def namedb_update_must_equal( rec, change_fields ):
    """
    Generate the set of fields that must stay the same across an update.
    """
    
    must_equal = []
    if len(change_fields) != 0:
        given = rec.keys()
        for k in given:
            if k not in change_fields:
                must_equal.append(k)

    return must_equal


def namedb_delete_prepare( cur, primary_key, primary_key_value, table_name ):
    """
    Prepare to delete a record, but make sure the fields in record
    correspond to actual columns.
    
    Return a DELETE FROM ... WHERE statement on success.
    Raise an Exception if not.

    DO NOT CALL THIS METHOD DIRETLY
    """

    # primary key corresponds to a real column 
    namedb_assert_fields_match( cur, {primary_key: primary_key_value}, table_name, columns_match_record=False )

    query = "DELETE FROM %s WHERE %s = ?;" % (table_name, primary_key)
    values = (primary_key_value,)
    return (query, values)


def namedb_query_execute( cur, query, values ):
    """
    Execute a query.  If it fails, exit.

    DO NOT CALL THIS DIRECTLY.
    """

    # log.debug("%s", "".join( ["%s %s" % (frag, "'%s'" % val if type(val) in [str, unicode] else val) for (frag, val) in zip(query.split("?"), values + ("",))] ))

    try:
        ret = cur.execute( query, values )
        return ret
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to execute query (%s, %s)" % (query, values))
        log.error("\n".join(traceback.format_stack()))
        sys.exit(1)


def namedb_preorder_insert( cur, preorder_rec ):
    """
    Add a name or namespace preorder record, if it doesn't exist already.

    DO NOT CALL THIS DIRECTLY.
    """

    preorder_row = copy.deepcopy( preorder_rec )
    
    assert 'preorder_hash' in preorder_row, "BUG: missing preorder_hash"

    try:
        preorder_query, preorder_values = namedb_insert_prepare( cur, preorder_row, "preorders" )
    except Exception, e:
        log.exception(e)
        log.error("FATAL: Failed to insert name preorder '%s'" % preorder_row['preorder_hash']) 
        sys.exit(1)

    namedb_query_execute( cur, preorder_query, preorder_values )
    return True


def namedb_preorder_remove( cur, preorder_hash ):
    """
    Remove a preorder hash.

    DO NOT CALL THIS DIRECTLY.
    """

    try:
        query, values = namedb_delete_prepare( cur, 'preorder_hash', preorder_hash, 'preorders' )
    except Exception, e:
        log.exception(e)
        log.error("FATAL: Failed to delete preorder with hash '%s'" % preorder_hash )
        sys.exit(1)

    namedb_query_execute( cur, query, values )
    return True


def namedb_name_fields_check( name_rec ):
    """
    Make sure that a name record has some fields
    that must always be present:
    * name
    * namespace_id
    * name_hash128

    Makes the record suitable for insertion/update.
    NOTE: MODIFIES name_rec
    """

    if not name_rec.has_key('name'):
        raise Exception("BUG: name record has no name")

    # extract namespace ID if it's not already there 
    if not name_rec.has_key('namespace_id'):
        name_rec['namespace_id'] = get_namespace_from_name( name_rec['name'] )

    # extract name_hash if it's not already there 
    if not name_rec.has_key('name_hash128'):
        name_rec['name_hash128'] = hash256_trunc128( name_rec['name'] )

    return True


def namedb_name_insert( cur, input_name_rec ):
    """
    Add the given name record to the database,
    if it doesn't exist already.
    """
   
    name_rec = copy.deepcopy( input_name_rec )
    namedb_name_fields_check( name_rec )

    try:
        query, values = namedb_insert_prepare( cur, name_rec, "name_records" )
    except Exception, e:
        log.exception(e)
        log.error("FATAL: Failed to insert name '%s'" % name_rec['name'])
        sys.exit(1)

    namedb_query_execute( cur, query, values )

    return True


def namedb_name_update( cur, opcode, input_opdata, only_if={}, constraints_ignored=[] ):
    """
    Update an existing name in the database.
    If non-empty, only update the given fields.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    opdata = copy.deepcopy( input_opdata )
    namedb_name_fields_check( opdata )
    mutate_fields = op_get_mutate_fields( opcode )

    if opcode not in OPCODE_CREATION_OPS:
        assert 'name' not in mutate_fields, "BUG: 'name' listed as a mutate field for '%s'" % (opcode)

    # reduce opdata down to the given fields....
    must_equal = namedb_update_must_equal( opdata, mutate_fields )
    must_equal += ['name']

    for ignored in constraints_ignored:
        if ignored in must_equal:
            # ignore this constraint 
            must_equal.remove( ignored )

    try:
        query, values = namedb_update_prepare( cur, 'name', opdata, "name_records", must_equal=must_equal, only_if=only_if )
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to update name '%s'" % opdata['name'])
        sys.exit(1)

    namedb_query_execute( cur, query, values )

    try:
        assert cur.rowcount == 1, "Updated %s row(s)" % cur.rowcount 
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to update name '%s'" % opdata['name'])
        log.error("Query: %s", "".join( ["%s %s" % (frag, "'%s'" % val if type(val) in [str, unicode] else val) for (frag, val) in zip(query.split("?"), values + ("",))] ))
        sys.exit(1)

    return True


def namedb_namespace_fields_check( namespace_rec ):
    """
    Given a namespace record, make sure the following fields are present:
    * namespace_id
    * buckets

    Makes the record suitable for insertion/update.
    NOTE: MODIFIES namespace_rec
    """

    assert namespace_rec.has_key('namespace_id'), "BUG: namespace record has no ID"

    # make buckets into a JSON string 
    assert namespace_rec.has_key('buckets'), "BUG: no namespace price buckets"
    assert type(namespace_rec['buckets']) == list, "BUG: namespace buckets type %s, expected 'list'" % (type(namespace_rec['buckets']))

    bucket_str = json.dumps( namespace_rec['buckets'] )
    namespace_rec['buckets'] = bucket_str
    return namespace_rec


def namedb_namespace_insert( cur, input_namespace_rec ):
    """
    Add a namespace to the database,
    if it doesn't exist already.
    It must be a *revealed* namespace, not a ready namespace
    (to mark a namespace as ready, you should use the namedb_apply_operation()
    method).
    """

    namespace_rec = copy.deepcopy( input_namespace_rec )
    namedb_namespace_fields_check( namespace_rec )

    try:
        query, values = namedb_insert_prepare( cur, namespace_rec, "namespaces" )
    except Exception, e:
        log.exception(e)
        log.error("FATAL: Failed to insert revealed namespace '%s'" % namespace_rec['namespace_id']) 
        sys.exit(1)

    namedb_query_execute( cur, query, values )
    return True


def namedb_namespace_update( cur, opcode, input_opdata, only_if={}, constraints_ignored=[] ):
    """
    Make a namespace ready.
    Only works if the namespace is *not* ready.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    opdata = copy.deepcopy( input_opdata )
    assert opdata.has_key('namespace_id'), "BUG: namespace record has no ID"

    mutate_fields = op_get_mutate_fields( opcode ) 
    
    assert 'namespace_id' not in mutate_fields, "BUG: 'name' listed as a mutate field for '%s'" % (opcode)

    # reduce opdata down to the given fields....
    must_equal = namedb_update_must_equal( opdata, mutate_fields )
    must_equal += ['namespace_id']
 
    for ignored in constraints_ignored:
        if ignored in must_equal:
            # ignore this constraint 
            must_equal.remove( ignored )

    try:
        query, values = namedb_update_prepare( cur, 'namespace_id', opdata, "namespaces", must_equal=must_equal, only_if={} )
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to update name '%s'" % opdata['namespace_id'])
        sys.exit(1)

    namedb_query_execute( cur, query, values )

    try:
        assert cur.rowcount == 1, "Updated %s row(s)" % cur.rowcount 
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to update name '%s'" % opdata['name'])
        log.error("Query: %s", "".join( ["%s %s" % (frag, "'%s'" % val if type(val) in [str, unicode] else val) for (frag, val) in zip(query.split("?"), values + ("",))] ))
        sys.exit(1)

    return True
    

def namedb_op_sanity_check( opcode, op_data, record ):
    """
    Sanity checks over operation and state graph data:
    * opcode and op_data must be consistent
    * record must have an opcode
    * the given opcode must be reachable from it.
    """

    assert op_data.has_key('op'), "BUG: operation data is missing its 'op'"
    op_data_opcode = op_get_opcode_name( op_data['op'] )

    assert record.has_key('op'), "BUG: current record is missing its 'op'"
    cur_opcode = op_get_opcode_name( record['op'] )

    assert op_data_opcode is not None, "BUG: undefined operation '%s'" % op_data['op']
    assert cur_opcode is not None, "BUG: undefined current operation '%s'" % record['op']

    if op_data_opcode != opcode:
        # only allowed of the serialized opcode is the same
        # (i.e. as is the case for register/renew)
        assert NAME_OPCODES.get( op_data_opcode, None ) is not None, "BUG: unrecognized opcode '%s'" % op_data_opcode
        assert NAME_OPCODES.get( opcode, None ) is not None, "BUG: unrecognized opcode '%s'" % opcode 

        assert NAME_OPCODES[op_data_opcode] == NAME_OPCODES[opcode], "BUG: %s != %s" % (opcode, op_data_opcode)

    assert opcode in OPCODE_SEQUENCE_GRAPH, "BUG: impossible to arrive at operation '%s'" % opcode
    assert cur_opcode in OPCODE_SEQUENCE_GRAPH, "BUG: impossible to have processed operation '%s'" % cur_opcode
    assert opcode in OPCODE_SEQUENCE_GRAPH[ cur_opcode ], "BUG: impossible sequence from '%s' to '%s'" % (cur_opcode, opcode)

    return True


def namedb_state_mutation_sanity_check( opcode, op_data ):
    """
    Make sure all mutate fields for this operation are present.
    Return True if so
    Raise exception if not
    """

    # sanity check:  each mutate field in the operation must be defined in op_data, even if it's null.
    missing = []
    mutate_fields = op_get_mutate_fields( opcode )
    for field in mutate_fields:
        if field not in op_data.keys():
            missing.append( field )

    assert len(missing) == 0, ("BUG: operation '%s' is missing the following fields: %s" % (opcode, ",".join(missing)))
    return True


def namedb_state_transition_sanity_check( opcode, op_data, history_id, cur_record, record_table ):
    """
    Sanity checks: make sure that:
    * the opcode and op_data are consistent with one another.
    * the history_id, cur_record, and record_table are consistent with one another.

    Return True if so.
    Raise an exception if not.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    namedb_op_sanity_check( opcode, op_data, cur_record )

    if opcode in OPCODE_NAME_STATE_TRANSITIONS:
        # name state transition 
        assert record_table == "name_records", "BUG: name state transition opcode (%s) on table %s" % (opcode, record_table)
        assert cur_record.has_key('name'), "BUG: name state transition with no name"
        assert cur_record['name'] == history_id, "BUG: history ID '%s' != '%s'" % (history_id, cur_record['name'])

    elif opcode in OPCODE_NAMESPACE_STATE_TRANSITIONS:
        # namespace state transition 
        assert record_table == "namespaces", "BUG: namespace state transition opcode (%s) on table %s" % (opcode, record_table)
        assert cur_record.has_key('namespace_id'), "BUG: namespace state transition with no namespace ID"
        assert cur_record['namespace_id'] == history_id, "BUG: history ID '%s' != '%s'" % (history_id, cur_record['namespace_id'])

    return True


def namedb_state_transition( cur, opcode, op_data, block_id, vtxindex, txid, history_id, cur_record, record_table, constraints_ignored=[] ):
    """
    Given an operation (opcode, op_data), a point in time (block_id, vtxindex, txid), and a current
    record (history_id, cur_record), apply the operation to the record and save the delta to the record's
    history.  Also, insert or update the new record into the db.

    The cur_record must exist already.

    Return the newly updated record on success.
    Raise an exception on failure.

    DO NOT CALL THIS METHOD DIRECTLY.
    """
    
    # sanity check: must be a state-transitioning operation
    try:
        assert opcode in OPCODE_NAME_STATE_TRANSITIONS + OPCODE_NAMESPACE_STATE_TRANSITIONS, "BUG: opcode '%s' is not a state-transition"
    except Exception, e:
        log.exception(e)
        log.error("BUG: opcode '%s' is not a state-transition operation" % opcode)
        sys.exit(1)

    # sanity check make sure we got valid state transition data
    try:
        rc = namedb_state_transition_sanity_check( opcode, op_data, history_id, cur_record, record_table )
        if not rc:
            raise Exception("State transition sanity checks failed")

        rc = namedb_state_mutation_sanity_check( opcode, op_data )
        if not rc:
            raise Exception("State mutation sanity checks failed")

    except Exception, e:
        log.exception(e)
        log.error("FATAL: state transition sanity checks failed")
        sys.exit(1)

    # back these fields up... 
    rc = namedb_history_save( cur, opcode, history_id, block_id, vtxindex, txid, cur_record )
    if not rc:
        log.error("FATAL: failed to save history for '%s' at (%s, %s)" % (history_id, block_id, vtxindex))
        sys.exit(1)

    # make sure we have a name 
    op_data_name = copy.deepcopy( op_data )

    rc = False
    if opcode in OPCODE_NAME_STATE_TRANSITIONS:
        # name state transition 
        op_data_name['name'] = history_id
        rc = namedb_name_update( cur, opcode, op_data_name, constraints_ignored=constraints_ignored )

    elif opcode in OPCODE_NAMESPACE_STATE_TRANSITIONS:
        # namespace state transition 
        op_data_name['namespace_id'] = history_id
        rc = namedb_namespace_update( cur, opcode, op_data_name, constraints_ignored=constraints_ignored )
    
    if not rc:
        log.error("FATAL: opcode is not a state-transition operation")
        sys.exit(1)

    # success!
    return True
    

def namedb_state_create_sanity_check( opcode, op_data, history_id, preorder_record, record_table ):
    """
    Sanity checks on a preorder and a state-creation operation:
    * the opcode must match the op_data
    * the history_id and operation must match the preorder
    * everything must match the record table.

    Return True on success
    Raise an exception on error.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    namedb_op_sanity_check( opcode, op_data, preorder_record )
    preorder_opcode = op_get_opcode_name(preorder_record['op'])

    if opcode in OPCODE_NAME_STATE_CREATIONS:
        # name state transition 
        assert record_table == "name_records", "BUG: name state transition opcode (%s) on table %s" % (opcode, record_table)
        assert preorder_opcode in OPCODE_NAME_STATE_PREORDER, "BUG: preorder record opcode '%s' is not a name preorder" % (preorder_opcode)

    elif opcode in OPCODE_NAMESPACE_STATE_CREATIONS:
        # namespace state transition 
        assert record_table == "namespaces", "BUG: namespace state transition opcode (%s) on table %s" % (opcode, record_table)
        assert preorder_opcode in OPCODE_NAMESPACE_STATE_PREORDER, "BUG: preorder record opcode '%s' is not a namespace preorder" % (preorder_opcode)

    return True


def namedb_state_create( cur, opcode, new_record, block_id, vtxindex, txid, history_id, preorder_record, record_table ):
    """
    Given an operation and a new record (opcode, new_record), a point in time (block_id, vtxindex, txid), and a preorder
    record for a known record (history_id, preorder_record), create the initial name or namespace using
    the preorder and operation's data.  Record the preorder as history.

    The record named by history_id must not exist.

    Return True on success
    Raise an exception on failure.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    # sanity check: must be a state-creation operation 
    if opcode not in OPCODE_NAME_STATE_CREATIONS + OPCODE_NAMESPACE_STATE_CREATIONS or opcode in OPCODE_NAME_STATE_IMPORTS:
        log.error("FATAL: Opcode '%s' is not a state-creating operation" % opcode)
        sys.exit(1)

    try:
        assert 'preorder_hash' in preorder_record.keys(), "BUG: no preorder hash"
    except Exception, e:
        log.exception(e)
        log.error("FATAL: no preorder hash")
        sys.exit(1)
        
    try:
        # sanity check to make sure we got valid state-creation data
        rc = namedb_state_create_sanity_check( opcode, new_record, history_id, preorder_record, record_table )
        if not rc:
            raise Exception("state-creation sanity check on '%s' failed" % opcode )

        rc = namedb_state_mutation_sanity_check( opcode, new_record )
        if not rc:
            raise Exception("State mutation sanity checks failed")

    except Exception, e:
        log.exception(e)
        log.error("FATAL: state-creation sanity check failed")
        sys.exit(1)

    # save the preorder as history 
    rc = namedb_history_save( cur, opcode, history_id, block_id, vtxindex, txid, preorder_record )
    if not rc:
        log.error("FATAL: failed to save history for '%s' at (%s, %s)" % (history_id, block_id, vtxindex))
        sys.exit(1)

    rc = False
    if opcode in OPCODE_NAME_STATE_CREATIONS:
        # name state transition 
        rc = namedb_name_insert( cur, new_record )

    elif opcode in OPCODE_NAMESPACE_STATE_CREATIONS:
        # namespace state transition 
        rc = namedb_namespace_insert( cur, new_record )
    
    if not rc:
        log.error("FATAL: opcode is not a state-creation operation")
        sys.exit(1)

    # clear the associated preorder 
    rc = namedb_preorder_remove( cur, preorder_record['preorder_hash'] )
    if not rc:
        log.error("FATAL: failed to remove preorder")
        sys.exit(1)

    # success!
    return True


def namedb_name_import_sanity_check( cur, opcode, op_data, history_id, block_id, vtxindex, prior_import, record_table):
    """
    Sanity checks on a name-import:
    * the opcode must match the op_data
    * everything must match the record table.
    * if prior_import is None, then the name shouldn't exist
    * if prior_import is not None, then it must exist

    Return True on success
    Raise an exception on error.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    assert opcode in OPCODE_NAME_STATE_IMPORTS, "BUG: opcode '%s' does not import a name" % (opcode)
    assert record_table == "name_records", "BUG: wrong table %s" % record_table
    assert namedb_is_history_snapshot( op_data ), "BUG: import is incomplete"
    
    namedb_op_sanity_check( opcode, op_data, op_data )

    # must be the only such existant name, if prior_import is None
    name = namedb_get_name( cur, history_id, block_id )
    if prior_import is None:
        assert name is None, "BUG: trying to import '%s' for the first time, again" % history_id
    else:
        assert name is not None, "BUG: trying to overwrite non-existent import '%s'" % history_id
        assert prior_import['name'] == history_id, "BUG: trying to overwrite import for different name '%s'" % history_id
        
        # must actually be prior
        assert prior_import['block_number'] < block_id or (prior_import['block_number'] == block_id and prior_import['vtxindex'] < vtxindex), \
                "BUG: prior_import comes after op_data"

    return True


def namedb_state_create_as_import( db, opcode, new_record, block_id, vtxindex, txid, history_id, prior_import, record_table ):
    """
    Given an operation and a new record (opcode, new_record), and point in time (block_id, vtxindex, txid)
    create the initial name as an import.  Does not work on namespaces.

    The record named by history_id must not exist if prior_import is None.
    The record named by history_id must exist if prior_import is not None.

    Return True on success
    Raise an exception on failure.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    # sanity check: must be a name, and must be an import
    if opcode not in OPCODE_NAME_STATE_IMPORTS:
        log.error("FATAL: Opcode '%s' is not a state-importing operation" % opcode)
        sys.exit(1)

    try:
        cur = db.cursor()

        # sanity check to make sure we got valid state-import data
        rc = namedb_name_import_sanity_check( cur, opcode, new_record, history_id, block_id, vtxindex, prior_import, record_table )
        if not rc:
            raise Exception("state-import sanity check on '%s' failed" % opcode )

        rc = namedb_state_mutation_sanity_check( opcode, new_record )
        if not rc:
            raise Exception("State mutation sanity checks failed")

    except Exception, e:
        log.exception(e)
        log.error("FATAL: state-import sanity check failed")
        sys.exit(1)

    cur = db.cursor()

    if prior_import is None:
        # duplicate as history
        rec_dup = copy.deepcopy(new_record)
        rec_dup['history_snapshot'] = True
        rc = namedb_history_save( cur, opcode, history_id, block_id, vtxindex, txid, rec_dup )
        if not rc:
            log.error("FATAL: failed to save history snapshot for '%s' at (%s, %s)" % (history_id, block_id, vtxindex))
            sys.exit(1)
        
        cur = db.cursor()

        # save for the first time
        rc = namedb_name_insert( cur, new_record )

    else:
        # save the prior import
        rc = namedb_history_save( cur, opcode, history_id, block_id, vtxindex, txid, prior_import )
        if not rc:
            log.error("FATAL: failed to save history snapshot for '%s' at (%s, %s)" % (history_id, block_id, vtxindex))
            sys.exit(1)
        
        cur = db.cursor()
        rc = namedb_name_update( cur, opcode, new_record )

    if not rc:
        log.error("FATAL: failed to execute import operation")
        sys.exit(1)

    # success!
    return True


def namedb_is_history_snapshot( history_snapshot ):
    """
    Given a dict and a history ID, verify that it is a history snapshot.
    It must have all consensus fields.
    Return True if so.
    Raise an exception of it doesn't.
    """
    
    # sanity check:  each mutate field in the operation must be defined in op_data, even if it's null.
    missing = []

    assert 'op' in history_snapshot.keys(), "BUG: no op given"

    opcode = op_get_opcode_name( history_snapshot['op'] )
    assert opcode is not None, "BUG: unrecognized op '%s'" % history_snapshot['op']

    consensus_fields = op_get_consensus_fields( opcode )
    for field in consensus_fields:
        if field not in history_snapshot.keys():
            missing.append( field )

    assert len(missing) == 0, ("BUG: operation '%s' is missing the following fields: %s" % (opcode, ",".join(missing)))
    return True


def namedb_state_create_from_prior_history( cur, opcode, new_record, block_id, vtxindex, txid, history_id, history_snapshot, preorder_record, record_table ):
    """
    Given an operation and a new record (opcode, new_record), a point in time (block_id, vtxindex, txid), and a prior historic
    snapshot of the record for a known record (history_id, history_snapshot), create the initial name or namespace using
    the history snapshot and operation's data.  Record the history snapshot as the most recent history item.

    The record named by history_id must exist.

    Return True on success
    Raise an exception on failure.

    DO NOT CALL THIS METHOD DIRECTLY.
    """

    # sanity check: must be a state-creation operation 
    if opcode not in OPCODE_NAME_STATE_CREATIONS + OPCODE_NAMESPACE_STATE_CREATIONS:
        log.error("FATAL: Opcode '%s' is not a state-creating operation" % opcode)
        sys.exit(1)
       
    try:
        assert 'preorder_hash' in preorder_record.keys(), "BUG: no preorder hash"
        assert 'block_number' in preorder_record.keys(), "BUG: preorder has no block number"
        assert 'vtxindex' in preorder_record.keys(), "BUG: preorder has no vtxindex"
        assert 'txid' in preorder_record.keys(), "BUG: preorder has no txid"
    except Exception, e:
        log.exception(e)
        log.error("FATAL: no preorder hash")
        sys.exit(1)

    preorder_block_id = preorder_record['block_number']
    preorder_vtxindex = preorder_record['vtxindex']
    preorder_txid = preorder_record['txid']
    
    try:
      
        assert preorder_block_id in history_snapshot.keys(), "BUG: no history snapshot at %s" % block_id
        assert len(history_snapshot[preorder_block_id]) > 0, "BUG: no history at %" % block_id

        # verify that this is a whole record 
        rc = namedb_is_history_snapshot( history_snapshot[preorder_block_id][-1] )
        if not rc:
            raise Exception("History snapshot sanity checks failed")

        assert 'history_snapshot' in history_snapshot[preorder_block_id][-1].keys(), "BUG: not a marked history snapshot"
        assert history_snapshot[preorder_block_id][-1]['history_snapshot'], "BUG: not a true history snapshot"

        rc = namedb_state_mutation_sanity_check( opcode, new_record )
        if not rc:
            raise Exception("State mutation sanity checks failed")

    except Exception, e:
        log.exception(e)
        log.error("FATAL: state-creation sanity check failed")
        sys.exit(1)

    # save the history snapshot at the preorder block/txindex/txid
    rc = namedb_history_save( cur, opcode, history_id, preorder_block_id, preorder_vtxindex, preorder_txid, history_snapshot[preorder_block_id][-1], history_snapshot=True )
    if not rc:
        log.error("FATAL: failed to save history snapshot for '%s' at (%s, %s)" % (history_id, preorder_block_id, preorder_vtxindex))
        sys.exit(1)

    # save the preorder as history at the current time
    rc = namedb_history_save( cur, opcode, history_id, block_id, vtxindex, txid, preorder_record )
    if not rc:
        log.error("FATAL: failed to save history for '%s' at (%s, %s)" % (history_id, block_id, vtxindex))
        sys.exit(1)

    # make sure we have a name 
    op_data_name = copy.deepcopy( new_record )

    rc = False
    if opcode in OPCODE_NAME_STATE_CREATIONS:
        # name state transition 
        op_data_name['name'] = history_id
        rc = namedb_name_update( cur, opcode, op_data_name )

    elif opcode in OPCODE_NAMESPACE_STATE_CREATIONS:
        # namespace state transition 
        op_data_name['namespace_id'] = history_id
        rc = namedb_namespace_update( cur, opcode, op_data_name )
   
    if not rc:
        log.error("FATAL: opcode is not a state-creation operation")
        sys.exit(1)

    # success!
    return True


def namedb_history_save( cur, opcode, history_id, block_id, vtxindex, txid, input_rec, history_snapshot=False ):
    """
    Given a current record and an operation to perform on it,
    calculate and save its history diff (i.e. all the fields that
    the operation will change).
    @history_id is either the name or namespace ID

    Return True on success
    Raise an Exception on error
    """

    history_diff = None 

    log.debug("SAVE HISTORY %s AT (%s, %s)" % (history_id, block_id, vtxindex))

    # special case: if the given record was created by an operation
    # whose mutate fields are "__all__", then *everything* must be
    # snapshotted, regardless of what the operation changes.
    prev_opcode = op_get_opcode_name( input_rec['op'] )
    prev_history_diff_fields = op_get_backup_fields( prev_opcode )
    if '__all__' in prev_history_diff_fields or history_snapshot:

        # full back-up of this record
        history_diff_fields = op_get_consensus_fields( prev_opcode )
        history_snapshot = True

        history_diff = dict( [(field, input_rec[field]) for field in history_diff_fields] )
        history_diff['history_snapshot'] = True

    else:
        
        # field-by-field backup of this record
        # sanity check... 
        history_diff_fields = op_get_backup_fields( opcode )

        # sanity check
        missing = []
        for field in history_diff_fields:
            if not input_rec.has_key( field ):
                missing.append( field )

        assert len(missing) == 0, "BUG: missing history diff fields '%s'" % ",".join(missing)

        history_diff = dict( [(field, input_rec.get(field, None)) for field in history_diff_fields] )

    rc = namedb_history_append( cur, history_id, block_id, vtxindex, txid, history_diff )
    if not rc:
        raise Exception("Failed to save history for '%s' at %s" % (history_rec, block_id))

    return True


def namedb_history_append( cur, history_id, block_id, vtxindex, txid, history_rec ):
    """
    Append a history record at the given (block_id, vtxindex) point in time, for a
    record with the primary key @history_id.

    DO NOT CALL THIS DIRECTLY; USE namedb_history_save()
    """

    assert 'vtxindex' in history_rec, "Malformed history record at %s: missing vtxindex" % block_id
    assert 'txid' in history_rec, "Malformed history record at (%s,%s): missing txid" % (block_id, history_rec['vtxindex'])
    assert 'op' in history_rec, "Malformed history record at (%s,%s): missing op" % (block_id, history_rec['vtxindex'])
    
    record_txt = json.dumps( history_rec )

    history_insert = {
        "txid": txid,
        "history_id": history_id,
        "block_id": block_id,
        "vtxindex": vtxindex,
        "op": history_rec['op'],
        "history_data": record_txt
    }

    try:
        query, values = namedb_insert_prepare( cur, history_insert, "history" )
    except Exception, e:
        log.exception(e)
        log.error("FATAL: failed to append history record for '%s' at (%s, %s)" % (history_id, block_id, vtxindex))
        sys.exit(1)

    namedb_query_execute( cur, query, values )
    return True


def namedb_get_history_range( cur, name, start_block_id, end_block_id ):
    """
    Get the history for a name over a range of blocks.
    Returns a list of history dicts.
    """

    select_query = "SELECT * FROM history WHERE history_id = ? AND block_id >= ? AND block_id < ? ORDER BY block_id, vtxindex ASC;"
    history_rows = namedb_query_execute( cur, select_query, (name, start_block_id, end_block_id) )

    return namedb_history_extract( history_rows )


def namedb_get_history( cur, history_id ):
    """
    Get all of the history for a name or namespace.
    """

    # get history in increasing order by block_id and then vtxindex
    select_query = "SELECT * FROM history WHERE history_id = ? ORDER BY block_id, vtxindex ASC;"
    history_rows = namedb_query_execute( cur, select_query, (history_id,) )
    
    return namedb_history_extract( history_rows )


def namedb_history_extract( history_rows ):
    """
    Given the rows of history for a name, collapse
    them into a history dictionary.
    Return a dict of:
    {
        block_id: [
            { ... historical data ...
             txid:
             vtxindex:
             op:
             opcode: 
            }, ...
        ],
        ...
    }
    """

    history = {}
    for history_row in history_rows:

        block_id = history_row['block_id']
        data_json = history_row['history_data']
        hist = json.loads( data_json )
        
        hist[ 'opcode' ] = op_get_opcode_name( hist['op'] )

        if history.has_key( block_id ):
            history[ block_id ].append( hist )
        else:
            history[ block_id ] = [ hist ]

    return history


def namedb_get_namespace( cur, namespace_id, current_block, include_history=True ):
    """
    Get an unexpired namespace (revealed or ready) and optionally its history.
    """

    select_query = "SELECT * FROM namespaces WHERE namespace_id = ? AND " + \
                   "((op = ? AND namespaces.reveal_block <= ? AND ? < namespaces.reveal_block + ?) OR " + \
                   " (op = ?))"
    namespace_rows = namedb_query_execute( cur, select_query, (namespace_id, NAMESPACE_REVEAL, current_block, current_block, NAMESPACE_REVEAL_EXPIRE, NAMESPACE_READY))

    namespace_row = namespace_rows.fetchone()
    if namespace_row is None:
        # no such namespace 
        return None 

    namespace = {}
    namespace.update( namespace_row )

    if include_history:
        hist = namedb_get_history( cur, namespace_id )
        namespace['history'] = hist

    # convert buckets back to list 
    buckets = json.loads( namespace['buckets'] )
    namespace['buckets'] = buckets

    return namespace


def namedb_get_namespace_by_preorder_hash( cur, preorder_hash, include_history=True ):
    """
    Get a namespace by its preorder hash (regardless of whether or not it was expired.)
    """

    select_query = "SELECT * FROM namespaces WHERE preorder_hash = ?;"
    namespace_rows = namedb_query_execute( cur, select_query, (preorder_hash,))

    namespace_row = namespace_rows.fetchone()
    if namespace_row is None:
        # no such namespace 
        return None 

    namespace = {}
    namespace.update( namespace_row )

    if include_history:
        hist = namedb_get_history( cur, namespace['namespace_id'] )
        namespace['history'] = hist

    # convert buckets back to list 
    buckets = json.loads( namespace['buckets'] )
    namespace['buckets'] = buckets

    return namespace


def namedb_get_name_by_preorder_hash( cur, preorder_hash, include_history=True ):
    """
    Get a name by its preorder hash (regardless of whether or not it was expired or revoked.)
    """

    select_query = "SELECT * FROM name_records WHERE preorder_hash = ?;"
    name_rows = namedb_query_execute( cur, select_query, (preorder_hash,))

    name_row = name_rows.fetchone()
    if name_row is None:
        # no such preorder
        return None 

    namerec = {}
    namerec.update( name_row )

    if include_history:
        hist = namedb_get_history( cur, namerec['name'] )
        namerec['history'] = hist

    return namerec



def namedb_select_where_unexpired_names( current_block ):
    """
    Generate part of a WHERE clause that selects name records
    (or projections of them) that are not expired.
    """
    query_fragment = "name_records.first_registered <= ? AND " + \
                     "((namespaces.op = ? AND (namespaces.ready_block + namespaces.lifetime > ? OR name_records.last_renewed + namespaces.lifetime >= ?)) OR " + \
                     "(namespaces.op = ? AND namespaces.reveal_block <= ?))"

    query_args = (current_block, NAMESPACE_READY, current_block, current_block, NAMESPACE_REVEAL, current_block + NAMESPACE_REVEAL_EXPIRE )

    return (query_fragment, query_args)


def namedb_get_name( cur, name, current_block, include_expired=False, include_history=True ):
    """
    Get a name and all of its history.
    Return the name + history on success
    Return None if the name doesn't exist, or is expired (NOTE: will return a revoked name)
    """

    if not include_expired:

        unexpired_fragment, unexpired_args = namedb_select_where_unexpired_names( current_block )
        select_query = "SELECT name_records.* FROM name_records JOIN namespaces ON name_records.namespace_id = namespaces.namespace_id " + \
                       "WHERE name = ? AND " + unexpired_fragment + ";"
        args = (name, ) + unexpired_args

    else:
        select_query = "SELECT * FROM name_records WHERE name = ?;"
        args = (name,)

    name_rows = namedb_query_execute( cur, select_query, args )
    name_row = name_rows.fetchone()
    if name_row is None:
        # no such name
        return None 

    name_rec = {}
    name_rec.update( name_row )
    
    if include_history:
        name_history = namedb_get_history( cur, name )
        name_rec['history'] = name_history

    return name_rec


def namedb_get_preorder( cur, preorder_hash, current_block_number, include_expired=False, expiry_time=None ):
    """
    Get a preorder record by hash.
    If include_expired is set, then so must expiry_time
    Return None if not found.
    """

    select_query = None 
    args = None 

    if include_expired:
        select_query = "SELECT * FROM preorders WHERE preorder_hash = ?;"
        args = (preorder_hash,)

    else:
        assert expiry_time is not None, "expiry_time is required with include_expired"
        select_query = "SELECT * FROM preorders WHERE preorder_hash = ? AND block_number < ?;"
        args = (preorder_hash, expiry_time + current_block_number)

    preorder_rows = namedb_query_execute( cur, select_query, (preorder_hash,))
    preorder_row = preorder_row.fetchone()
    if preorder_row is None:
        # no such preorder 
        return None

    preorder_rec = {}
    preorder_rec.update( preorder_row )
    
    return preorder_rec


def namedb_get_names_owned_by_address( cur, address, current_block ):
    """
    Get the list of non-expired, non-revoked names owned by an address.
    Only works if there is a *singular* address for the name.
    """

    unexpired_fragment, unexpired_args = namedb_select_where_unexpired_names( current_block )

    select_query = "SELECT * FROM name_records JOIN namespaces ON name_records.namespace_id = namespaces.namespace_id " + \
                   "WHERE name_records.address = ? AND name_records.revoked = 0 AND " + unexpired_fragment + ";"
    args = (address, current_block) + unexpired_args

    name_rows = namedb_query_execute( cur, select_query, args )

    names = []
    for name_row in name_rows:
        names.append( name_row['name'] )

    if len(names) == 0:
        return None 
    else:
        return names


def namedb_restore_from_history( name_rec, block_id ):
    """
    Given a name or a namespace record, replay its
    history diffs "back in time" to a particular block
    number.

    Return the sequence of states the name record went
    through at that block number, starting from the beginning
    of the block.

    Return None if the record does not exist at that point in time

    The returned records will *not* have a 'history' key.
    """

    block_history = list( reversed( sorted( name_rec['history'].keys() ) ) )

    historical_rec = copy.deepcopy( name_rec )
    del historical_rec['history']

    if len(block_history) == 0:
        # there is no history here...
        try:
            assert namedb_is_history_snapshot( historical_rec ), "BUG: no history for incomplete name"
            return [historical_rec]
        except Exception, e:
            log.exception(e)
            log.debug("\n%s" % (json.dumps(historical_rec, indent=4, sort_keys=True)))
            log.error("FATAL: tried to restore history for incomplete record")
            sys.exit(1)


    if block_id > block_history[0]:
        # current record is valid
        return [historical_rec]

    if block_id < name_rec['block_number']:
        # doesn't yet exist
        return None

    # find the latest block prior to block_number
    last_block = len(block_history)
    for i in xrange( 0, len(block_history) ):
        if block_id >= block_history[i]:
            last_block = i
            break

    i = 0
    while i < last_block:

        try:
            diff_list = list( reversed( name_rec['history'][ block_history[i] ] ) )
        except:
            print json.dumps( name_rec['history'][block_history[i]] )
            raise

        for diff in diff_list:

            if diff.has_key('history_snapshot'):
                # wholly new state
                historical_rec = copy.deepcopy( diff )
                del historical_rec['history_snapshot']

            else:
                # delta in current state
                # no matter what, 'block_number' cannot be altered (unless it's a history snapshot)
                if diff.has_key('block_number'):
                    del diff['block_number']

                historical_rec.update( diff )

        i += 1

    # if this isn't the earliest history element, and the next-earliest
    # one (at last block) has multiple entries, then generate the sequence
    # of updates for all but the first one.  This is because all but the
    # first one were generated in the same block (i.e. the block requested).
    updates = [ copy.deepcopy( historical_rec ) ]

    if i < len(block_history):

        try:
            diff_list = list( reversed( name_rec['history'][ block_history[i] ] ) )
        except:
            print json.dumps( name_rec['history'][block_history[i]] )
            raise

        if len(diff_list) > 1:
            for diff in diff_list[:-1]:

                # no matter what, 'block_number' cannot be altered
                if diff.has_key('block_number'):
                    del diff['block_number']

                if diff.has_key('history_snapshot'):
                    # wholly new state
                    historical_rec = copy.deepcopy( diff )
                    del historical_rec['history_snapshot']

                else:
                    # delta in current state
                    historical_rec.update( diff )

                updates.append( copy.deepcopy(historical_rec) )

    return list( reversed( updates ) )



def namedb_rec_restore( db, rows, history_id_key, block_id, include_history=False ):
    """
    Restore a record to its previous states over a block.
    Return the list of previous states (not sorted; you can do so on vtxindex if you want).
    """

    def get_history( history_id ):
        hist_cur = db.cursor()
        hist = namedb_get_history( hist_cur, history_id )
        return hist

    ret = []
    
    for row in rows:
        rec = {}
        rec.update( row )

        rec_history = get_history( rec[history_id_key] )
        rec['history'] = rec_history

        restored_recs = namedb_restore_from_history( rec, block_id )
        if include_history:
            for r in restored_recs:
                r['history'] = rec_history

        ret += restored_recs

    return ret


def namedb_get_all_records_at( db, block_id, include_history=False ):
    """
    Get the states that each name and namespace record
    passed through in the given block.

    Return the list of prior record states, ordered by vtxindex.
    """
    
    ret = []
    
    # all name records preordered for the first time at this block 
    cur = db.cursor()
    name_preorder_rows_query = "SELECT * FROM name_records " + \
                               "WHERE (name_records.block_number = ? OR name_records.preorder_block_number = ?);"

    name_preorder_rows = namedb_query_execute( cur, name_preorder_rows_query, (block_id,block_id))

    restored_recs = namedb_rec_restore( db, name_preorder_rows, "name", block_id, include_history=include_history )
    ret += restored_recs
    log.debug("%s name-preorder states at %s" % (len(restored_recs), block_id ))

    # all name records affected by this block (including re-preorders, but excluding initial preorders)
    cur = db.cursor()
    name_rows_query = "SELECT name_records.* FROM name_records JOIN history ON name_records.name = history.history_id " + \
                      "WHERE name_records.block_number < ? AND name_records.preorder_block_number != ? AND history.block_id = ? GROUP BY name_records.name;"

    name_rows = namedb_query_execute( cur, name_rows_query, (block_id,block_id,block_id))

    restored_recs = namedb_rec_restore( db, name_rows, "name", block_id, include_history=include_history )
    ret += restored_recs
    log.debug("%s name-change states at %s" % (len(restored_recs), block_id ))


    # all outstanding preorders created at this block
    cur = db.cursor()
    preorder_rows_query = "SELECT * FROM preorders WHERE block_number = ?;"
    preorder_rows = namedb_query_execute( cur, preorder_rows_query, (block_id,))

    cnt = 0
    for preorder in preorder_rows:

        preorder_rec = {}
        preorder_rec.update( preorder )

        ret.append( preorder_rec )
        cnt += 1

    log.debug("%s preorders created at %s" % (cnt, block_id))


    # all namespaces preordered at this block
    cur = db.cursor()
    namespace_preorder_rows_query = "SELECT * FROM namespaces WHERE namespaces.block_number = ?;"
    namespace_preorder_rows = namedb_query_execute( cur, namespace_preorder_rows_query, (block_id,))

    restored_recs = namedb_rec_restore( db, namespace_preorder_rows, "namespace_id", block_id, include_history=include_history )
    ret += restored_recs
    log.debug("%s namespace-preorder states at %s" % (len(restored_recs), block_id ))


    # all namespaces revealed/readied at this block
    cur = db.cursor()
    namespace_rows_query = "SELECT namespaces.* FROM namespaces JOIN history ON namespaces.namespace_id = history.history_id " + \
                           "WHERE namespaces.block_number <= ? AND history.block_id = ? AND (namespaces.op = ? OR namespaces.op = ?);"
    namespace_rows = namedb_query_execute( cur, namespace_rows_query, (block_id,block_id,NAMESPACE_REVEAL,NAMESPACE_READY))

    restored_recs = namedb_rec_restore( db, namespace_rows, "namespace_id", block_id, include_history=include_history )
    ret += restored_recs
    log.debug("%s namespace-change states at %s" % (len(restored_recs), block_id ))

    return sorted( ret, key=lambda n: n['vtxindex'] )


def namedb_get_all_names( cur, current_block, offset=None, count=None ):
    """
    Get a list of all names in the database, optionally
    paginated with offset and count.  Exclude expired names.
    """

    args = [current_block]
    query = "SELECT * FROM name_records JOIN namespaces ON name_records.namespace_id = namespaces.namespace_id WHERE last_renewed + lifetime < ? ORDER BY name"

    if count is not None:
        query += " LIMIT ?"
        args.append( count )

    if offset is not None:
        query += " OFFSET ?"
        args.appnd( offset )

    name_rows = namedb_query_execute( cur, query, tuple(args) )
    ret = []
    for name_row in name_rows:
        rec = {}
        rec.update( name_row )
        ret.append( rec )

    return ret 


def namedb_get_names_in_namespace( cur, namespace_id, current_block, offset=None, count=None ):
    """
    Get a list of all names in a namespace, optionally
    paginated with offset and count.  Exclude expired names
    """

    args = [namespace_id, current_block]
    query = "SELECT * FROM name_records JOIN namespaces ON name_records.namespace_id = namespaces.namespace_id WHERE namespace_id = ? AND last_renewed + lifetime < ? ORDER BY name"

    if count is not None:
        query += " LIMIT ?"
        args.append( count )

    if offset is not None:
        query += " OFFSET ?"
        args.append( offset )

    name_rows = namedb_query_execute( cur, query, tuple(args) )
    ret = []
    for name_row in name_rows:
        rec = {}
        rec.update( name_row )
        ret.append( rec )

    return ret


def namedb_get_all_namespace_ids( cur ):
    """
    Get a list of all READY namespace IDs.
    """

    query = "SELECT namespace_id FROM namespaces WHERE op = ?;"
    args = (NAMESPACE_READY,)

    namespace_rows = namedb_query_execute( cur, query, args )
    ret = []
    for namespace_row in namespace_rows:
        ret.append( namespace_row['name'] )

    return ret


def namedb_get_all_preordered_namespace_hashes( cur, current_block ):
    """
    Get a list of all preordered namespace hashes that haven't expired yet.
    """

    query = "SELECT preorder_hash FROM preorders WHERE op = ? AND block_number >= ? AND block_number < ?;"
    args = (NAMESPACE_PREORDER, current_block, current_block + NAMESPACE_PREORDER_EXPIRE )

    namespace_rows = namedb_query_execute( cur, query, args )
    ret = []
    for namespace_row in namespace_rows:
        ret.append( namespace_row['preorder_hash'] )

    return ret


def namedb_get_all_revealed_namespace_ids( self, current_block ):
    """
    Get all non-expired revealed namespaces.
    """
    
    query = "SELECT namespace_id FROM namespaces WHERE op = ? AND reveal_block < ?;"
    args = (NAMESPACE_REVEAL, current_block + NAMESPACE_REVEAL_EXPIRE )

    namespace_rows = namedb_query_execute( cur, query, args )
    ret = []
    for namespace_row in namespace_rows:
        ret.append( namespace_row['namespace_id'] )

    return ret


def namedb_get_all_importing_namespace_hashes( self, current_block ):
    """
    Get the list of all non-expired preordered and revealed namespace hashes.
    """

    query = "SELECT preorder_hash FROM namespaces WHERE (op = ? AND reveal_block < ?) OR (op = ? AND block_number < ?);"
    args = (NAMESPACE_REVEAL, current_block + NAMESPACE_REVEAL_EXPIRE, NAMESPACE_PREORDER, current_block + NAMESPACE_PREORDER_EXPIRE )

    namespace_rows = namedb_query_execute( cur, query, args )
    ret = []
    for namespace_row in namespace_rows:
        ret.append( namespace_row['preorder_hash'] )

    return ret


def namedb_get_names_by_sender( cur, sender, current_block ):
    """
    Given a sender pubkey script, find all the non-expired non-revoked names owned by it.
    Return None if the sender owns no names.
    """

    unexpired_query, unexpired_args = namedb_select_where_unexpired_names( current_block )

    query = "SELECT name_records.name FROM name_records JOIN namespaces ON name_records.namespace_id = namespaces.namespace_id " + \
            "WHERE name_records.sender = ? AND name_records.revoked = 0 AND " + unexpired_query + ";"

    args = (sender,) + unexpired_args

    name_rows = namedb_query_execute( cur, query, args )
    names = []

    for name_row in name_rows:
        names.append( name_row['name'] )

    return names
    

def namedb_get_name_preorder( db, preorder_hash, current_block ):
    """
    Get a (singular) name preorder record outstanding at the given block, given the preorder hash.

    Return the preorder record on success.
    Return None if not found.
    """

    select_query = "SELECT * FROM preorders WHERE preorder_hash = ? AND op = ? AND block_number < ?;"
    args = (preorder_hash, NAME_PREORDER, current_block + NAME_PREORDER_EXPIRE)

    cur = db.cursor()
    preorder_rows = namedb_query_execute( cur, select_query, args )
    
    preorder_row = preorder_rows.fetchone()
    if preorder_row is None:
        # no such preorder 
        return None 

    preorder_rec = {}
    preorder_rec.update( preorder_row )

    unexpired_query, unexpired_args = namedb_select_where_unexpired_names( current_block )

    # make sure that the name doesn't already exist 
    select_query = "SELECT name_records.preorder_hash " + \
                   "FROM name_records JOIN namespaces ON name_records.namespace_id = namespaces.namespace_id " + \
                   "WHERE name_records.preorder_hash = ? AND " + \
                   unexpired_query + ";"

    args = (preorder_hash,) + unexpired_args
    
    cur = db.cursor()
    nm_rows = namedb_query_execute( cur, select_query, args )

    nm_row = nm_rows.fetchone()
    if nm_row is not None:
        # name with this preorder exists 
        return None 

    return preorder_rec


def namedb_get_namespace_preorder( db, namespace_preorder_hash, current_block ):
    """
    Get a namespace preorder, given its hash.

    Return the preorder record on success.
    Return None if not found, or if it expired, or if the namespace was revealed or readied.
    """

    cur = db.cursor()
    select_query = "SELECT * FROM preorders WHERE preorder_hash = ? AND op = ? AND block_number < ?;"
    args = (namespace_preorder_hash, NAMESPACE_PREORDER, current_block + NAMESPACE_PREORDER_EXPIRE)
    preorder_rows = namedb_query_execute( cur, select_query, args )

    preorder_row = preorder_rows.fetchone()
    if preorder_row is None:
        # no such preorder 
        return None 

    preorder_rec = {}
    preorder_rec.update( preorder_row )

    # make sure that the namespace doesn't already exist 
    cur = db.cursor()
    select_query = "SELECT preorder_hash FROM namespaces WHERE preorder_hash = ? AND ((op = ?) OR (op = ? AND reveal_block < ?));"
    args = (namespace_preorder_hash, NAMESPACE_READY, NAMESPACE_REVEAL, current_block + NAMESPACE_REVEAL_EXPIRE)
    ns_rows = namedb_query_execute( cur, select_query, args )

    ns_row = ns_rows.fetchone()
    if ns_row is not None:
        # exists
        return None 

    return preorder_rec


def namedb_get_namespace_reveal( cur, namespace_id, current_block, include_history=True ):
    """
    Get a namespace reveal, and optionally its history, given its namespace ID.
    Only return a namespace record if:
    * it is not ready
    * it is not expired
    """

    select_query = "SELECT * FROM namespaces WHERE namespace_id = ? AND op = ? AND reveal_block <= ? AND ? < reveal_block + ?;"
    args = (namespace_id, NAMESPACE_REVEAL, current_block, current_block, NAMESPACE_REVEAL_EXPIRE)
    namespace_reveal_rows = namedb_query_execute( cur, select_query, args )

    namespace_reveal_row = namespace_reveal_rows.fetchone()
    if namespace_reveal_row is None:
        # no such reveal 
        return None 

    reveal_rec = {}
    reveal_rec.update( namespace_reveal_row )

    if include_history:
        hist = namedb_get_history( cur, namespace_id )
        reveal_rec['history'] = hist

    # convert buckets back to list 
    buckets = json.loads( reveal_rec['buckets'] )
    reveal_rec['buckets'] = buckets

    return reveal_rec


def namedb_get_namespace_ready( cur, namespace_id, include_history=True ):
    """
    Get a ready namespace, and optionally its history.
    Only return a namespace if:
    * it is ready
    """

    select_query = "SELECT * FROM namespaces WHERE namespace_id = ? AND op = ?;"
    namespace_rows = namedb_query_execute( cur, select_query, (namespace_id, NAMESPACE_READY))

    namespace_row = namespace_rows.fetchone()
    if namespace_row is None:
        # no such namespace 
        return None 

    namespace = {}
    namespace.update( namespace_row )

    if include_history:
        hist = namedb_get_history( cur, namespace_id )
        namespace['history'] = hist

    # convert buckets back to list 
    buckets = json.loads( namespace['buckets'] )
    namespace['buckets'] = buckets

    return namespace


def namedb_get_name_from_name_hash128( cur, name_hash128, block_number ):
    """
    Given the hexlified 128-bit hash of a name, get the name.
    """

    unexpired_query, unexpired_args = namedb_select_where_unexpired_names( block_number )

    select_query = "SELECT name FROM name_records JOIN namespaces ON name_records.namespace_id = namespaces.namespace_id " + \
                   "WHERE name_hash128 = ? AND revoked = 0 AND " + unexpired_query + ";"

    args = (name_hash128,) + unexpired_args
    name_rows = namedb_query_execute( cur, select_query, args )

    name_row = name_rows.fetchone()
    if name_row is None:
        # no such namespace 
        return None 

    return name_row['name']


if __name__ == "__main__":
    # basic unit tests
    import random 

    path = "/tmp/namedb.sqlite"
    if not os.path.exists( path ):
        db = namedb_create( path )
    else:
        db = namedb_open( path )

    name = "test%s.test" % random.randint( 0, 2**32 )
    sender = "76a9147144b3fef9fe537e2445f1c0dfb4ce007c51461288ac"
    sender_pubkey = "046a6582a6566aa4059b7361536e7e4ac3df4d77bf6e843c4c8207eaa12e0ca19e15fc59c959b4a5d6d1de975ab059d9255a795dd57b9c78656a070ea5002efe87"
    sender_address = "1BKufFedDrueBBFBXtiATB2PSdsBGZxf3N"
    recipient = "76a914d3d4a11953ce8ba01b08548997830c11b1ad9a7288ac"
    recipient_pubkey = "04f52b0c1558202cb4403faacb6a74ad8a0d23538f448184b1c8ce80a0325aad9af0877061c2fd72cebec8524a99951d5834a8b8b96ddf4e2d582ee9dd61864dae"
    recipient_address = "1LK4JDfxaYZjJAinao3q5KdrLCtW3AFeQ6"
    current_block_number = 373610
    prior_block_number = 373601
    txid = "ce99f01aa17995c77e041beee93cf3bcf47ef68d18c16f79edd120a204d7c808"
    vtxindex = 1
    op = NAME_REGISTRATION
    opcode = "NAME_REGISTRATION"
    op_fee = 640000
    consensus_hash = "54a451b8a09a2acd951b06bda2b8e69f"

    namespace_address = "12HcV1f7XtQTgSPt7r1mpyr1ppfnX8fPa4"
    namespace_base = 4
    namespace_block_number = 373500
    namespace_buckets = [6,5,4,3,2,1,0,0,0,0,0,0,0,0,0,0]
    namespace_coeff = 250
    namespace_lifetime = 520000
    namespace_id = "test"
    namespace_id_hash = "6134faee8737a865995aa5423b55f1a8ec69fe4b"
    namespace_no_vowel_discount = 10
    namespace_nonalpha_discount = 10
    namespace_ready_block = 373600
    namespace_recipient = "76a914b7e40511f53f69045cb14c6c5a714d6a4ffe3a3788ac"
    namespace_recipient_address = "1HmKpCXiK4ExFbdTG1Y38jcrtx9KPkgYKX"
    namespace_reveal_block = 373510
    namespace_sender = "76a914b7e40511f53f69045cb14c6c5a714d6a4ffe3a3788ac"
    namespace_sender_pubkey = "04d6fd1ba0effaf1e8d94ea7b7a3d0ef26fea00a14ce5ffcc1495fe588a2c6d0f35eaa22c01a35bee5817f03d769a3a38a3bb50182c61449ad125555daf26396fb"
    namespace_txid = "71754175ee3168ade90b74c78af58312708a7103fd2d8d17346cad7a49b934da"
    namespace_version = 1

    namespace_record = {
        "address": namespace_address,
        "base": namespace_base,
        "block_number": namespace_reveal_block,
        "buckets": namespace_buckets,
        "coeff": namespace_coeff,
        "lifetime": namespace_lifetime,
        "namespace_id": namespace_id,
        "no_vowel_discount": namespace_no_vowel_discount,
        "nonalpha_discount": namespace_nonalpha_discount,
        "preorder_hash": namespace_id_hash,
        "ready_block": namespace_ready_block,
        # "opcode": "NAMESPACE_READY",
        "op": NAMESPACE_REVEAL,
        "op_fee": 6140,
        "recipient": namespace_recipient,
        "recipient_address": namespace_recipient_address,
        "reveal_block": namespace_reveal_block,
        "sender": namespace_sender,
        "sender_pubkey": namespace_sender_pubkey,
        "txid": namespace_txid,
        "vtxindex": 3,
        "version": namespace_version
    }
    
    preorder_record = {
        # NOTE: was preorder_name_hash
        "preorder_hash": hash_name( name, sender, recipient_address ),
        "consensus_hash": consensus_hash,
        "block_number": prior_block_number,
        "sender": sender,
        "sender_pubkey": sender_pubkey,
        "address": "1Nrmkp6rhebJnL2wURkUrwH93Evaq1s3Yd",
        "fee": 12345,
        "op": NAME_PREORDER,
        "txid": "69c21d76a98dd450305200346602d38c2ee2c401a81acd3dbe9ead850ab6bc7b",
        "vtxindex": 20,
        "op_fee": 6400001
    }

    name_record = {
        'name': name,
        "preorder_hash": hash_name( name, sender, recipient_address ),
        'value_hash': None,             # i.e. the hex hash of profile data in immutable storage.
        'sender': str(recipient),       # the recipient is the expected future sender
        'sender_pubkey': str(recipient_pubkey),
        'address': str(recipient_address),

        'block_number': prior_block_number,
        'preorder_block_number': preorder_record['block_number'],
        'first_registered': current_block_number,
        'last_renewed': current_block_number,
        'revoked': False,

        'op': op,
        'txid': txid,
        'vtxindex': vtxindex,
        'op_fee': op_fee,

        # (not imported)
        'importer': None,
        'importer_address': None,
        'consensus_hash': preorder_record['consensus_hash']
    }

    namespace_preorder_record = {
        "address": pybitcoin.script_hex_to_address( namespace_record['sender'] ),
        "block_number": 373400,
        "consensus_hash": "1c54465d3486f07be2c7a81af0ef44ad",
        "fee": 6160,
        "preorder_hash": namespace_id_hash,
        "op": NAMESPACE_PREORDER,
        "op_fee": 40000000,
        "sender": namespace_record['sender'],
        "sender_pubkey": namespace_record['sender_pubkey'],
        "txid": namespace_record['namespace_id'],
        "vtxindex": 3
    }

    name_update_op = {
        'op': NAME_UPDATE,
        'op_fee': 6140,
        'vtxindex': 4,
        'txid': '0e0a3ed6145b6424267ae042911fbfc69e21a17d8c579ac9b114ba934ccda950',
        'block_number': 373701,
        'consensus_hash': '4017d71d6c5e87c9efe8633f1dc1c425',
        'name_hash': hash256_trunc128( name_record['name'] + '4017d71d6c5e87c9efe8633f1dc1c425' ),
        'value_hash': '11' * 20,
    }

    cur = db.cursor()
    print "namespace preorder"
    namedb_preorder_insert( cur, namespace_preorder_record )
    db.commit()
    
    print "namespace reveal"
    ns_preorder_rec = namedb_get_preorder( cur, namespace_preorder_record['preorder_hash'], namespace_preorder_record['block_number'] )
    namedb_state_create( cur, "NAMESPACE_REVEAL", namespace_record, namespace_record['reveal_block'], namespace_record['vtxindex'], namespace_record['txid'], namespace_record['namespace_id'], ns_preorder_rec, "namespaces" )
    db.commit()
    
    print "name preorder"
    namedb_preorder_insert( cur, preorder_record )
    db.commit()

    print "name register"
    nm_preorder_rec = namedb_get_preorder( cur, preorder_record['preorder_hash'], preorder_record['block_number'] )
    print "preorder:\n%s\n" % json.dumps(nm_preorder_rec, indent=4)
    namedb_state_create( cur, "NAME_REGISTRATION", name_record, name_record['block_number'], name_record['vtxindex'], name_record['txid'], name_record['name'], nm_preorder_rec, 'name_records' )
    db.commit()

    print "name update"
    nm_rec = namedb_get_name( cur, name_record['name'], current_block=name_record['block_number'] )
    print "register:\n%s\n" % json.dumps(nm_rec, indent=4)
    namedb_state_transition( cur, "NAME_UPDATE", name_update_op, name_update_op['block_number'], name_update_op['vtxindex'], name_update_op['txid'], name_record['name'], name_record, 'name_records' )
    db.commit()

    nm_rec = namedb_get_name( cur, name_record['name'], current_block=name_record['block_number'] )
    print "after update:\n%s\n" % json.dumps(nm_rec, indent=4)

    print "\nhistory:\n"
    hist = namedb_get_history( cur, name )
    print json.dumps( hist, indent=4 )

    db.close()
