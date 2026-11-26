import json, os, tempfile, unittest
from pathlib import Path
import database
from route_optimizer import minutes, optimise, windows_for_date, OptimisationError
from datetime import date

class UpgradeTests(unittest.TestCase):
    def test_migrate_old_schema_and_settings(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as tmp:
            old=database.DB_PATH; database.DB_PATH=Path(tmp)/'routes.db'
            try:
                conn=sqlite3.connect(database.DB_PATH)
                conn.execute('CREATE TABLE locations (id INTEGER PRIMARY KEY,name TEXT,latitude REAL,longitude REAL,open_time TEXT,close_time TEXT,is_depot INTEGER DEFAULT 0)')
                conn.execute("INSERT INTO locations(name,latitude,longitude) VALUES('Outlet',35.9,14.5)")
                conn.commit();conn.close();database.init_db()
                self.assertEqual(len(database.all_locations()),1)
                self.assertEqual(database.all_locations()[0]['service_minutes'],5)
                database.save_setting('depot_json','{}')
                self.assertEqual(database.get_setting('depot_json'),'{}')
            finally: database.DB_PATH=old
    def test_time(self): self.assertEqual(minutes('09:15'),555)
    def test_unknown_hours_are_unrestricted_and_closed_is_closed(self):
        stops=[{'id':'TXP'}, {'id':1,'weekly_hours':json.dumps({'Thursday':'CLOSED'})}, {'id':'TXP'}]
        self.assertEqual(windows_for_date(stops,date(2026,11,12)),{1:[]})
        stops[1]['weekly_hours']='{}'
        self.assertEqual(windows_for_date(stops,date(2026,11,12)),{})
    def test_txp_is_fixed_first_and_last(self):
        stops=[{'id':'TXP','name':'TXP','service_minutes':0},{'id':1,'name':'Shop','service_minutes':5},{'id':'TXP','name':'TXP','service_minutes':0}]
        matrix=[[0,10,20],[10,0,10],[20,10,0]]
        result=optimise(stops,matrix,'08:00',12,{})
        self.assertEqual([x['id'] for x in result],['TXP',1,'TXP'])
        with self.assertRaises(OptimisationError):
            optimise([stops[1], stops[0], stops[2]], matrix, '08:00', 12, {})
        with self.assertRaises(OptimisationError):
            optimise([stops[0], stops[2], stops[1]], matrix, '08:00', 12, {})
    def test_split_opening_periods_do_not_allow_lunch_closure(self):
        stops=[{'id':'TXP','name':'TXP','service_minutes':0},{'id':1,'name':'Shop','service_minutes':5},{'id':'TXP','name':'TXP','service_minutes':0}]
        matrix=[[0,20,40],[20,0,20],[40,20,0]]
        windows={1:[(480,720),(840,1020)]}
        result=optimise(stops,matrix,'12:30',12,windows)
        self.assertGreaterEqual(minutes(result[1]['arrival']),840)

if __name__=='__main__': unittest.main()

class GoogleHoursTests(unittest.TestCase):
    def test_google_sunday_zero_and_omitted_minutes(self):
        from google_maps import _parse_periods
        data = {'regularOpeningHours': {'periods': [
            {'open': {'day': 1, 'hour': 8}, 'close': {'day': 1, 'hour': 19}},
            {'open': {'day': 6, 'hour': 9}, 'close': {'day': 6, 'hour': 13}},
            {'open': {'day': 6, 'hour': 16}, 'close': {'day': 6, 'hour': 19}},
        ]}}
        hours = _parse_periods(data)
        self.assertEqual(hours['Monday'], [['08:00', '19:00']])
        self.assertEqual(hours['Saturday'], [['09:00', '13:00'], ['16:00', '19:00']])
        self.assertEqual(hours['Sunday'], 'CLOSED')
    def test_missing_hours_not_invented(self):
        from google_maps import _parse_periods
        self.assertIsNone(_parse_periods({}))
