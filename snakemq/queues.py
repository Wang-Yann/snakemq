# -*- coding: utf-8 -*-
"""
Queues and persistent storage. TTL is decreased only by the disconnected time.
Queue manager "downtime" is not included.
"""

import time
import bisect
import sqlite3

###########################################################################
###########################################################################

FLAG_PERSISTENT = 1

###########################################################################
###########################################################################

class Item(object):
    def __init__(self, uuid, data, ttl, flags=0):
        self.uuid = uuid
        self.data = data
        self.ttl = ttl
        self.flags = flags

###########################################################################
###########################################################################

class Queue(object):
    def __init__(self, name, manager):
        self.name = name
        self.manager = manager
        self.queue = []
        self.last_disconnect_absolute = None

        self.load_persistent_data()
        self.disconnect()

    def load_persistent_data(self):
        self.queue[:] = self.manager.storage.get_items(self.name)            

    def connect(self):
        # remove outdated items and update TTL
        diff = time.time() - self.last_disconnect_absolute
        fresh_queue = []
        storage_update_ttls = []
        storage_to_delete = []
        for item in self.queue:
            item.ttl -= diff
            if item.ttl >= 0: # must include 0
                fresh_queue.append(item)
                if item.flags & FLAG_PERSISTENT:
                    storage_update_ttls.append(item)
            else:
                if item.flags & FLAG_PERSISTENT:
                    storage_to_delete.append(item)
        
        self.manager.storage.update_items_ttl(storage_update_ttls)
        self.manager.storage.delete_items(storage_to_delete)
        self.queue[:] = fresh_queue

    def disconnect(self):
        self.last_disconnect_absolute = time.time()

    def push(self, item):
        self.queue.append(item)
        if item.flags & FLAG_PERSISTENT:
            self.manager.storage.push(self.name, item)
        
    def get(self):
        """
        Get first item but do not remove it. Items are not outdated.
        @return: item or None if empty
        """
        # no need to test TTL because it is filtered in connect()
        if self.queue:
            return self.queue[0]
        else:
            return None

    def pop(self):
        """
        Remove first item.
        @return: None
        """
        if not self.queue:
            return
        item = self.queue.pop(0)
        if item.flags & FLAG_PERSISTENT:
            self.manager.storage.delete_items([item.uuid])

    def __len__(self):
        return len(self.queue)

###########################################################################
###########################################################################

class QueuesManager(object):
    def __init__(self, storage):
        """
        @param storage: persistent storage
        """
        self.storage = storage
        self.queues = {} #: name:Queue
        self.load_from_storage()

    def load_from_storage(self):
        for queue_name in self.storage.get_queues():
            self.get_queue(queue_name)

    def get_queue(self, queue_name):
        """
        @return: Queue
        """
        if queue_name in self.queues:
            queue = self.queues[queue_name]
        else:
            queue = Queue(queue_name, self)
            self.queues[queue_name] = queue
        return queue

    def cleanup(self):
        """
        remove empty queues
        """
        for queue_name, queue in self.queues.items():
            if not queue:
                del self.queues[queue_name]

    def close(self):
        """
        Delete queues and close persistent storage.
        """
        self.queues.clear()
        self.storage.close()
        self.storage = None

###########################################################################
###########################################################################

class SqliteQueuesStorage(object):
    def __init__(self, filename):
        self.conn = sqlite3.connect(filename)
        self.crs = self.conn.cursor()
        self.test_format()
        self.sweep()
    
    def close(self):
        self.crs.close()
        self.conn.close()

    def test_format(self):
        """
        Make sure that the database file content is OK.
        """
        self.crs.execute("""SELECT count(1) FROM sqlite_master
                                  WHERE type='table' AND name='items'""")
        if self.crs.fetchone()[0] != 1:
            self.prepare_format()

    def prepare_format(self):
        self.crs.execute("""CREATE TABLE items (queue_name TEXT, uuid TEXT,
                                                data TEXT, ttl REAL,
                                                flags INTEGER)""")
        self.conn.commit()

    def sweep(self):
        self.crs.execute("""VACUUM""")
        self.conn.commit()

    #######################################################

    def get_queues(self):
        """
        @return: list of queues names
        """
        self.crs.execute("""SELECT queue_name FROM items GROUP BY queue_name""")
        return [r[0] for r in self.crs.fetchall()]

    def get_items(self, queue_name):
        """
        @return: items of the queue
        """
        self.crs.execute("""SELECT uuid, data, ttl, flags FROM items
                                   WHERE queue_name = ?""",
                          (queue_name,))
        items = []
        for res in self.crs.fetchall():
            items.append(Item(res[0], res[1], res[2], res[3]))
        return items

    def push(self, queue_name, item):
        self.crs.execute("""INSERT INTO items
                                (queue_name, uuid, data, ttl, flags)
                                VALUES (?, ?, ?, ?, ?)""",
                      (queue_name, item.uuid, item.data, item.ttl, item.flags))
        self.conn.commit()

    def delete_items(self, items):
        # TODO use SQL operator "IN"
        for item in items:
            self.crs.execute("""DELETE FROM items WHERE uuid = ?""", 
                              (item.uuid,))
        self.conn.commit()

    def update_items_ttl(self, items):
        for item in items:
            self.crs.execute("""UPDATE items SET ttl = ? WHERE uuid = ?""",
                          (item.ttl, item.uuid))
        self.conn.commit()