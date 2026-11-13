"""Task dates use source timezone and calendar semantics, never opaque IDs."""
import shutil
import unittest
from tests import test_source_projection as projection


class ScheduleProjectionTests(unittest.TestCase):
    def test_schedule_metadata_is_exposed_without_rewriting_source(self):
        controller,row,_=projection.fixture()
        fields={'startDate':'2026-11-04T16:00:00.000+0000','dueDate':'2026-11-04T16:00:00.000+0000',
                'isAllDay':True,'timeZone':'Asia/Shanghai'}
        row['payload'].update(fields)
        task=controller.task(row)
        self.assertEqual(task.get('start_date'),fields['startDate'])
        self.assertEqual(task.get('due_date'),fields['dueDate'])
        self.assertIs(task.get('is_all_day'),True)
        self.assertEqual(task.get('time_zone'),'Asia/Shanghai')
        self.assertEqual(row['payload']['startDate'],fields['startDate'])


@unittest.skipUnless(shutil.which('node'),'Node required for UI dates')
class CardScheduleTests(unittest.TestCase):
    render_fixture=projection.SourceBrowserProjectionTests.render_fixture

    def render(self, **fields):
        task={'title':'外教英语','priority':3,'timeZone':'Asia/Shanghai',**fields}
        return self.render_fixture(task_fields=task,now='2026-11-07T00:00:00Z')

    def test_all_day_chinese_calendar_date_does_not_use_utc_day(self):
        ui=self.render(startDate='2026-11-04T16:00:00.000+0000',dueDate='2026-11-04T16:00:00.000+0000',isAllDay=True)
        self.assertIn('11.5 外教英语',ui['card'])
        self.assertIn('11.5 外教英语',ui['cardLabel'])
        self.assertNotIn('00:00',ui['card'])
        self.assertNotIn('#P:T',ui['card'])
        self.assertIn('p:t',ui['drawer'])

    def test_timed_task_shows_local_time(self):
        ui=self.render(title='干部例会',startDate='2026-11-13T04:20:00.000+0000',isAllDay=False)
        self.assertIn('11.13 干部例会',ui['card'])
        self.assertIn('12:20',ui['card'])

    def test_date_only_remains_calendar_day_in_western_timezone(self):
        ui=self.render(startDate='2026-11-05',timeZone='America/Los_Angeles',isAllDay=True)
        self.assertIn('11.5 外教英语',ui['card'])

    def test_source_timezone_overrides_browser_timezone(self):
        ui=self.render(startDate='2026-11-05T01:00:00Z',timeZone='America/New_York',isAllDay=False)
        self.assertIn('11.4 外教英语',ui['card'])
        self.assertIn('20:00',ui['card'])

    def test_missing_and_bad_dates_are_not_created_dates(self):
        for fields in ({},{'startDate':'not-a-date'},{'startDate':'2026-02-30'}):
            with self.subTest(fields=fields):
                ui=self.render(**fields)
                self.assertNotIn('Invalid Date',ui['card'])
                self.assertNotIn('1970',ui['card'])
                self.assertNotIn('#P:T',ui['card'])
                self.assertIn('日期',ui['card'])

    def test_next_year_and_ranges_are_explicit(self):
        ui=self.render(startDate='2026-12-31',dueDate='2027-01-02',isAllDay=True)
        self.assertIn('12.31–2027.1.2 外教英语',ui['card'])
        ui=self.render(startDate='2026-11-05T01:00:00Z',dueDate='2026-11-05T03:30:00Z',isAllDay=False)
        self.assertIn('09:00–11:30',ui['card'])

    def test_priority_is_rendered_as_color_not_hexadecimal_text(self):
        for priority,tone,color in ((5,'high','#bf5148'),(3,'medium','#b68a27'),(1,'low','#4b78b3'),(0,'none','#8b9299')):
            with self.subTest(priority=priority):
                ui=self.render(priority=priority)
                self.assertEqual(ui.get('cardColor'),color)
                self.assertNotIn(color,ui['card'])
                if priority:self.assertIn('badge priority-'+tone,ui['cardClasses'])


if __name__=='__main__':unittest.main()
