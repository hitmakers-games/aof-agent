"""Typing commitment follows commentary, never reasoning or a final control marker."""
import unittest
from unittest.mock import Mock, patch
from worker import app_server


class RespondingTests(unittest.TestCase):
    def test_commentary_commits_before_completion_but_control_reply_does_not(self):
        for phase, text in [('commentary','Working on it'),('final','[[SILENT]]'),('final','[[REACT:👍]]')]:
            with self.subTest(phase=phase,text=text):
                client=Mock()
                item={'type':'agentMessage','phase':phase,'text':text}
                client.notifications=[
                    {'method':'item/started','params':{'item':{'type':'reasoning'}}},
                    {'method':'item/started','params':{'item':item}},
                    {'method':'item/completed','params':{'item':item}},
                    {'method':'turn/completed','params':{'turn':{'status':'completed'}}}]
                client.request.side_effect=[{}, {'thread':{'id':'thread'}}, {'turn':{'id':'turn'}}]
                responding=Mock()
                with patch.object(app_server,'Client',return_value=client):
                    self.assertEqual(app_server.run([],{},'.','model','medium','hello',responding=responding),text)
                self.assertEqual(responding.call_count,int(phase=='commentary'))
