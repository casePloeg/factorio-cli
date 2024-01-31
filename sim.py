"""Factorio simulation"""

# stdlib imports
import functools
import json
from collections import defaultdict, Counter
from types import SimpleNamespace
import math

# project imports
from errors import * 
from files import load_files
from init import *
from utils import *
from craft import *
from data import *

# operations:
# craft
# next
# place
# mine
# research

# questions: 
# production
# state
# valid operation?

class Sim():
    def __init__(self, data_dict):
        self.data = SimpleNamespace(**data_dict)
        self.clear()

    def craft(self, item, amount):
        res, missing, available, msg = craftable(self, item, amount)
        if res == 0:
            missing[item] = amount
            av_sh = convert_to_sh(available)
            self.deduct_list(av_sh)
            self.place_in_inventory(self.data.recipes[item]['products'][0]['name'], amount)
            time_spent = self.craft_time_list(missing)
            print(time_spent)
            # put excess production into player inventory based on bulk orders 
            self.grant_excess_production(missing)
            if time_spent > 0:
                self.next(time_spent)
            return 0, None
        elif res == 2:
            return 1, f'crafting {amount} {item} failed, {msg}'
        else:
            return 1, f'something went wrong, {msg}'

    def next(self, seconds, check_rates=False):
        # simulate factory production, moving forwards in time
        # depends on current state of inventory before next() was called.
        # next(60) + next(60) != next(120), because state changes after each call 
        def produce(ci):
            def miner_potential(miner, item, amount, seconds):
                return (self.data.mining_drills[miner]['mining_speed']
                      * amount 
                      * seconds)
            def assembler_potential(assembler, item, amount, seconds):
                return (self.data.assemblers[assembler]['crafting_speed'] 
                      * amount 
                      * self.data.recipes[item]['products'][0]['amount'] 
                      * (seconds // self.data.recipes[item]['energy']))
            def furnace_potential(furnace, item, amount, seconds):
                return (self.data.furnaces[furnace]['crafting_speed']
                      * amount
                      * (seconds // self.data.recipes[item]['energy']))
            def machine_craft(item, num_produced, ci):
                wish = {item: {'name': item, 'amount': num_produced}}
                if item not in self.data.resources:
                    self.deduct_list(shopping_list(self.data.recipes, wish, 0), ci)
                    self.place_in_inventory(self.data.recipes[item]['products'][0]['name'], num_produced, ci)
                else:
                    self.place_in_inventory(item, num_produced, ci)
            def miner_actual(item, potential):
                return potential 
            def assembler_actual(item, potential):
                # respect rate limits
                if item in self.limited_items:
                    potential = min(potential, self.limited_items[item] - ci[item])
                # find actual production rate 
                wish = {item: {'name': item, 'amount': potential}}
                while not self.check_list(shopping_list(self.data.recipes, wish, 0), ci):
                    wish[item]['amount'] -= 1
                return wish[item]['amount']
            def furnace_actual(item, potential):
                return assembler_actual(item, potential) 
            prod_rates = defaultdict(lambda: defaultdict(int))
            machine_groups = zip([self.miners, self.assemblers, self.furnaces], [x for x in range(3)])
            calc_actual = {0: miner_actual, 1: assembler_actual, 2: furnace_actual}
            calc_potential = {0: miner_potential, 1: assembler_potential, 2: furnace_potential}
            # core algo: for each machine: potential -> actual -> craft
            for machine_group, key in machine_groups: 
                for machine_item_key, amount in machine_group.items():
                    item, machine = machine_item_key.split(':')
                    potential = calc_potential[key](machine, item, amount, seconds)  
                    actual = calc_actual[key](item, potential) 
                    prod_rates[item]['potential'] += potential 
                    prod_rates[item]['actual'] += actual
                    machine_craft(item, actual, ci)
            return prod_rates
        # ---- end of produce() helper function                    
        if check_rates:
            ci = self.current_items.copy()
        else:
            # move time forwards and commit to item changes
            self.game_time += seconds
            ci = self.current_items
        return produce(ci)

    def place_machine(self, machine, item, amount=1):
        def store(machine, item, amount):
            machine_types = [
              (self.data.mining_drills, 0),
              (self.data.assemblers, 1),
              (self.data.furnaces, 2),
            ]
            storage = {0: self.miners, 1: self.assemblers, 2: self.furnaces}
            for mt, key in machine_types:
                if machine in mt:
                    storage[key][f'{item}:{machine}'] += amount
        res, msg = self.deduct_item(machine, amount)
        if res != 0:
            return res, f'failed to place {amount} of {machine}, {msg}'
        res, msg = self.is_machine_compatible(machine, item)
        if res != 0:
            self.place_in_inventory(machine, amount)
            return res, f'failed to place {machine} producing {item}, {msg}'
        store(machine, item, amount)
        return 0, None

    def mine(self, resource, amount):
        if resource in {'stone', 'coal', 'iron-ore', 'copper-ore', 'crude-oil'}:
            self.place_in_inventory(resource, amount)
            time_spent = self.data.resources[resource]['mineable_properties']['mining_time'] * amount
            self.next(time_spent)
            return 0, None
        else:
            return 1, f'{resource} cannot be mined'

    def research(self, tech):
        # research a given technology, raise exception if potions not available
        # or given technology can not be researched yet
        res, msg = self.researchable(tech)
        if res == 0:
            pl = get_potion_list(self.data.technology, tech)
            self.deduct_list(pl)
            self.current_tech.add(tech)
            # unlock recipes
            for effect in self.data.technology[tech]['effects']:
                if effect['type'] == 'unlock-recipe':
                    self.current_recipes.add(effect['recipe'])
            return 0, None
        else:
            return res, msg

    # return True iff players have more than or equal to `amount` of given `item` in their
    # inventory
    def check_item(self, item, amount, ci=None, ret_missing=False):
        if ci == None:
            ci = self.current_items
        res = ci[item] >= amount 
        if ret_missing:
            return res, max(0, amount - ci[item])
        else:
            return res

    def check_list(self, sh, ci=None, ret_missing=False):
        def reduce_sh(accum, x):
            res, missing = accum
            r, m = self.check_item(x['name'], x['amount'], ci, ret_missing=True)
            res = r and res
            missing[x['name']] = m
            return res, missing
            
        if ci == None:
            ci = self.current_items
        if ret_missing:
            vals = [True, dict()]
            res, missing = functools.reduce(lambda x, y: reduce_sh(x, y), sh.values(), vals)
            return 0 if res else 1, missing 
        else:
            return functools.reduce(lambda x, y: x and self.check_item(y['name'], y['amount'], ci), sh.values(), True) 

    def deduct_item(self, item, amount, ci=None):
        if ci == None:
            ci = self.current_items
        if self.check_item(item, amount):
            ci[item] -= amount
            return 0, None
        else:
            return 1, f'player has < {amount} of {item} in inventory'

    def deduct_list(self, sh, ci=None):
        if ci == None:
            ci = self.current_items
        for k, v in sh.items():
            ci[k] -= v['amount']

    def place_in_inventory(self, item, amount, ci=None):
        if ci == None:
            ci = self.current_items
        # enfore integral system - avoid very real issues
        ci[item] += int(amount)

    # todo: add option for partial crafting, so if a player wants to craft 5 miners
    # but only has materials to make 3, the system will craft 3 miners and give a
    # warning that 2 could not be crafted because of resource constraints
    def craft_time_list(self, craft_list):
        time = 0
        for name, amount in craft_list.items():
            time += craft_time(self.data, name, amount) 
            print(time, name, amount)
        return time


    # TODO: is this correct?
    def grant_excess_production(self, craft_list):
        for name, amount in craft_list.items():
            ratio = self.data.recipes[name]['main_product']['amount']
            if amount % ratio != 0:
                self.place_in_inventory(name, (ratio * (1 + (amount // ratio))) - amount)

    def set_limit(self, item, amount):
        self.limited_items[item] = amount

    def preqs_researched(self, tech):
        preq = self.data.technology[tech]['prerequisites']    
        return functools.reduce(lambda x, y: x and y in self.current_tech, preq, True)

    def researchable(self, tech):
        """Decide whether a given technology can be researched or not"""
        if tech not in self.data.technology:
            return 1, f'researchable - {tech} could not be found in the list of tecnologies'
        if tech in self.current_tech:
            return 1, f'researchable - {tech} has already been researched'
        pl = get_potion_list(self.data.technology, tech)
        preq = self.data.technology[tech]['prerequisites']    
        if not self.preqs_researched(tech):
            return 1, f'researchable - one or more prerequisite technologies for {tech} have not been researched'
        res, missing = self.check_list(pl, ret_missing=True) 
        if res != 0: 
            return res, f'researchable - missing the potions required to research {tech}, {missing}'
        return 0, None

    # find all technologies that could be researched next 
    def all_researchable(self):
        res = set()
        for tech in self.data.technology:
            if tech not in self.current_tech and self.preqs_researched(tech):
                res.add(tech)
        return res

    def get_starter_recipes(self):
        enabled = set()
        for key, value in self.data.recipes.items():
          if value['enabled']:
            enabled.add(key)
        return enabled
          
    def clear(self):
        self.game_time = 0
        self.current_tech = get_starter_tech() 
        self.current_recipes = self.get_starter_recipes() 
        self.current_items = get_starter_inventory() 
        self.miners = defaultdict(int)
        self.assemblers = defaultdict(int)
        self.furnaces = defaultdict(int)
        self.limited_items = dict() 

    def update_state(self, game_time, current_tech, current_recipes, current_items, miners, assemblers, furnaces, limited_items):
        self.game_time = game_time 
        self.current_tech = current_tech 
        self.current_recipes = current_recipes 
        self.current_items = current_items
        self.miners = miners 
        self.assemblers = assemblers 
        self.furnaces = furnaces 
        self.limited_items = limited_items 

    def serialize_state(self):
        # every field is sorted so that this function is deterministic. 
        # the same actions performed in a simulation should produce the exact same
        # save file every time! Important for running tests
        def get_state():
            return {
                'game_time': self.game_time,
                'current_tech': sorted(list(self.current_tech)),
                'current_recipes': sorted(list(self.current_recipes)),
                'current_items': dict(sorted(self.current_items.items())),
                'miners': dict(sorted(self.miners.items())),
                'assemblers': dict(sorted(self.assemblers.items())),
                'furnaces': dict(sorted(self.furnaces.items())),
                'limited_items': dict(sorted(self.limited_items))
            }
        # Sort the outer dictionary and ensure inner dictionaries are sorted as well
        s = {k: v if isinstance(v, (int, str, list, float)) else dict(sorted(v.items())) for k, v in get_state().items()}
        return json.dumps(s, sort_keys=True)

    def deserialize_state(self, s_json):
        s = json.loads(s_json)
        self.game_time = s['game_time']
        self.current_tech = set(s['current_tech'])
        self.current_recipes = set(s['current_recipes'])
        self.current_items = defaultdict(int, s['current_items'])
        self.miners = defaultdict(int, s['miners'])
        self.assemblers = defaultdict(int, s['assemblers'])
        self.furnaces = defaultdict(int, s['furnaces'])
        self.limited_items = s['limited_items']

    def machines(self):
        combined = defaultdict()
        combined.update(self.miners)
        combined.update(self.assemblers)
        combined.update(self.furnaces)
        return '\n'.join([f'{k} : {v}' for k,v in combined.items()])

    def production(self):
        production = self.next(60, True)
        data = [[k, v['actual'], v['potential'], self.current_items[k], (self.limited_items[k] if k in self.limited_items else '')] for k, v in production.items()]
        return data

    def is_recipe_unlocked(self, item):
        if item in self.current_recipes:
            return 0, None
        else:
            return 1, f'{item} is not unlocked'

    # each machine type can only interact with specific items
    # return errors for combinations that aren't allowed
    def is_machine_compatible(self, machine, item):
        if machine in self.data.mining_drills:
            item_category = self.data.resources[item]['resource_category']
            machine_categories = self.data.mining_drills[machine]['resource_categories']
            return is_mineable(item, item_category, machine_categories) 
        elif machine in self.data.assemblers: 
            res, msg = self.is_recipe_unlocked(item)
            if res == 0:
                item_category = self.data.recipes[item]['category']
                machine_categories = self.data.assemblers[machine]['crafting_categories']
                if item_category in machine_categories:
                    return 0, 'pog'
                else:
                    return 1, f'incompatible combination, item: {item} has crafting category {item_category} but machine {machine} has categories {machine_categories}'
            else:
                return res, msg
        elif machine in self.data.furnaces:
            return is_smeltable(item) 