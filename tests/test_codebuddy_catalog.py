import importlib
import unittest


class CodeBuddyCatalogTests(unittest.TestCase):
    def module(self):
        self.assertIsNotNone(importlib.util.find_spec('dispatcher.codebuddy_catalog'),'Live model catalogue reader missing')
        return importlib.import_module('dispatcher.codebuddy_catalog')

    def test_acp_ids_and_per_model_efforts_override_old_packaged_names(self):
        result={'models':{'availableModels':[
            {'modelId':'hy4free','name':'Hy4','_meta':{'effortOptions':[{'value':'high'}]}},
            {'modelId':'space-bunny','name':'Space-Bunny','_meta':{'effortOptions':[{'value':'low'},{'value':'max'}]}},
            {'modelId':'custom-id','name':'Custom'}]}}
        self.assertEqual(self.module().models_from_acp(result),[
            {'id':'hy4free','label':'Hy4','efforts':['high']},
            {'id':'space-bunny','label':'Space-Bunny','efforts':['low','max']},
            {'id':'custom-id','label':'Custom','efforts':[]}])

    def test_partial_invalid_or_duplicate_model_lists_are_not_published(self):
        valid={'modelId':'glm-5.3-flash','name':'GLM','_meta':{'effortOptions':[{'value':'low'}]}}
        for rows in ([],[valid,valid],[dict(valid,modelId='--bad')],[dict(valid,_meta={'effortOptions':[{'value':'ultra'}]})],
                     [dict(valid,_meta={'effortOptions':'low'})]):
            with self.subTest(rows=rows),self.assertRaises(ValueError):
                self.module().models_from_acp({'models':{'availableModels':rows}})


if __name__=='__main__':unittest.main()
