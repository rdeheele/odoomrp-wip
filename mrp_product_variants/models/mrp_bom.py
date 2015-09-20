# -*- encoding: utf-8 -*-
##############################################################################
#
#    This program is free software: you can redistribute it and/or modify
#    it under the terms of the GNU Affero General Public License as published
#    by the Free Software Foundation, either version 3 of the License, or
#    (at your option) any later version.
#
#    This program is distributed in the hope that it will be useful,
#    but WITHOUT ANY WARRANTY; without even the implied warranty of
#    MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#    GNU General Public License for more details.
#
#    You should have received a copy of the GNU Affero General Public License
#    along with this program.  If not, see http://www.gnu.org/licenses/.
#
##############################################################################

from openerp import models, fields, api, exceptions, tools, _
from openerp.addons.product import _common


class MrpBomLine(models.Model):
    _inherit = 'mrp.bom.line'

    product_id = fields.Many2one(required=False)
    product_template = fields.Many2one(comodel_name='product.template',
                                       string='Product')
    attribute_value_ids = fields.Many2many(
        domain="[('id', 'in', possible_values[0][2])]")
    possible_values = fields.Many2many(
        comodel_name='product.attribute.value',
        compute='_get_possible_attribute_values')

    @api.one
    @api.depends('product_id', 'product_template')
    def _get_product_category(self):
        self.product_uom_category = (self.product_id.uom_id.category_id or
                                     self.product_template.uom_id.category_id)

    product_uom_category = fields.Many2one(
        comodel_name='product.uom.categ', string='UoM category',
        compute="_get_product_category")
    product_uom = fields.Many2one(
        domain="[('category_id', '=', product_uom_category)]")

    @api.one
    @api.depends('bom_id.product_tmpl_id',
                 'bom_id.product_tmpl_id.attribute_line_ids')
    def _get_possible_attribute_values(self):
        attr_values = self.env['product.attribute.value']
        for attr_line in self.bom_id.product_tmpl_id.attribute_line_ids:
            attr_values |= attr_line.value_ids
        self.possible_values = attr_values.sorted()

    @api.multi
    def onchange_product_id(self, product_id, product_qty=0):
        res = super(MrpBomLine, self).onchange_product_id(
            product_id, product_qty=product_qty)
        if product_id:
            product = self.env['product.product'].browse(product_id)
            res['value']['product_template'] = product.product_tmpl_id.id
        return res

    @api.multi
    @api.onchange('product_template')
    def onchange_product_template(self):
        if self.product_template:
            self.product_uom = (self.product_id.uom_id or
                                self.product_template.uom_id)
            return {'domain': {'product_id': [('product_tmpl_id', '=',
                                               self.product_template.id)]}}
        return {'domain': {'product_id': []}}


class MrpBom(models.Model):
    _inherit = 'mrp.bom'

    @api.model
    def _bom_explode(self, bom, product, factor, properties=None, level=0,
                     routing_id=False, previous_products=None,
                     master_bom=None, production=None):
        result, result2 = self._bom_explode_variants(
            bom, product, factor, properties=properties, level=level,
            routing_id=routing_id, previous_products=previous_products,
            master_bom=master_bom, production=production)
        return result, result2

    @api.model
    def _bom_explode_variants(
            self, bom, product, factor, properties=None, level=0,
            routing_id=False, previous_products=None, master_bom=None,
            production=None):
        """ Finds Products and Work Centers for related BoM for manufacturing
        order.
        @param bom: BoM of particular product template.
        @param product: Select a particular variant of the BoM. If False use
                        BoM without variants.
        @param factor: Factor represents the quantity, but in UoM of the BoM,
                        taking into account the numbers produced by the BoM
        @param properties: A List of properties Ids.
        @param level: Depth level to find BoM lines starts from 10.
        @param previous_products: List of product previously use by bom explore
                        to avoid recursion
        @param master_bom: When recursion, used to display the name of the
                        master bom
        @return: result: List of dictionaries containing product details.
                 result2: List of dictionaries containing Work Center details.
        """
        routing_id = bom.routing_id.id or routing_id
        uom_obj = self.env["product.uom"]
        routing_obj = self.env['mrp.routing']
        master_bom = master_bom or bom

        def _factor(factor, product_efficiency, product_rounding):
            factor = factor / (product_efficiency or 1.0)
            factor = _common.ceiling(factor, product_rounding)
            if factor < product_rounding:
                factor = product_rounding
            return factor

        factor = _factor(factor, bom.product_efficiency, bom.product_rounding)

        result = []
        result2 = []

        routing = ((routing_id and routing_obj.browse(routing_id)) or
                   bom.routing_id or False)
        if routing:
            for wc_use in routing.workcenter_lines:
                wc = wc_use.workcenter_id
                d, m = divmod(factor, wc_use.workcenter_id.capacity_per_cycle)
                mult = (d + (m and 1.0 or 0.0))
                cycle = mult * wc_use.cycle_nbr
                result2.append({
                    'name': (tools.ustr(wc_use.name) + ' - ' +
                             tools.ustr(bom.product_tmpl_id.name_get()[0][1])),
                    'workcenter_id': wc.id,
                    'sequence': level + (wc_use.sequence or 0),
                    'cycle': cycle,
                    'hour': float(wc_use.hour_nbr * mult +
                                  ((wc.time_start or 0.0) +
                                   (wc.time_stop or 0.0) + cycle *
                                   (wc.time_cycle or 0.0)) *
                                  (wc.time_efficiency or 1.0)),
                })

        for bom_line_id in bom.bom_line_ids:
            if bom_line_id.date_start and \
                    (bom_line_id.date_start > fields.Date.context_today(self))\
                    or bom_line_id.date_stop and \
                    (bom_line_id.date_stop < fields.Date.context_today(self)):
                continue
            # all bom_line_id variant values must be in the product
            if bom_line_id.attribute_value_ids:
                production_attr_values = []
                if not product and production:
                    for attr_value in production.product_attributes:
                        production_attr_values.append(attr_value.value.id)
                    if (set(map(int, bom_line_id.attribute_value_ids or [])) -
                            set(map(int, production_attr_values))):
                        continue
                elif not product or\
                        (set(map(int, bom_line_id.attribute_value_ids or [])) -
                         set(map(int, product.attribute_value_ids))):
                    continue
            if previous_products and (bom_line_id.product_id.product_tmpl_id.id
                                      in previous_products):
                raise exceptions.Warning(
                    _('Invalid Action! BoM "%s" contains a BoM line with a'
                      ' product recursion: "%s".') %
                    (master_bom.name, bom_line_id.product_id.name_get()[0][1]))

            quantity = _factor(bom_line_id.product_qty * factor,
                               bom_line_id.product_efficiency,
                               bom_line_id.product_rounding)
            if not bom_line_id.product_id:
                if not bom_line_id.type != "phantom":
                    bom_id = self._bom_find(
                        product_tmpl_id=bom_line_id.product_template.id,
                        properties=properties)
                else:
                    bom_id = False
            else:
                bom_id = self._bom_find(product_id=bom_line_id.product_id.id,
                                        properties=properties)

            #  If BoM should not behave like PhantoM, just add the product,
            #  otherwise explode further
            if (bom_line_id.type != "phantom" and
                    (not bom_id or self.browse(bom_id).type != "phantom")):
                if not bom_line_id.product_id:
                    print 'est-ce que le bom line a des attributs?'
                    product_attributes = (
                        bom_line_id.product_template.
                        _get_product_attributes_inherit_dict(
                            production.product_attributes))
                    product = self.env['product.product']._product_find(
                        bom_line_id.product_template, product_attributes)
                    if bom_line_id.formula_text:
                        attributes_dict = {}
                        for attr in production.product_attributes:
                            attributes_dict[attr.attribute.name] = attr.value.name
                            if attr.custom_value:
                                attributes_dict[attr.attribute.name] = attr.custom_value
                        localdict = {
                                'self': self,
                                'a': attributes_dict,
                        }
                        try:
                            exec bom_line_id.formula_text in localdict
                        except KeyError:
                            print 'boum'
                            continue
                        try:
                            formula = localdict['result']
                        except KeyError:
                            print 'boum'
                        print 'formula ', formula
                        #{'longueur_coffre': 210.0}
                        print 'product_attributes ', product_attributes
                        pav_obj = self.env['product.attribute.value']
                        if not product_attributes:
                            quantity = formula['unite_mm']
                        for attr in product_attributes:
                            attribute = self.env['product.attribute'].browse(attr['attribute'])
                            print 'formula ', formula
                            for formula_attr in formula:
                                if formula_attr == attribute.name:
                                    if formula_attr in ['longueur_tablier','quantite_lames']:
                                        print 'formula_attr in longueur_tablier, quantite_lames'
                                        pav_id = pav_obj.search([('name','=',str(int(formula[formula_attr]))),('attribute_id','=',attribute.id)])
                                        if not pav_id:
                                            pav_id = pav_obj.create({'name': int(formula[formula_attr]),
                                                                     'attribute_id': attribute.id,
                                                                     'attribute_code': int(formula[formula_attr])})
                                        else:
                                            pav_id = pav_id[0]
                                        
                                        attr.update({'value': pav_id.id})
                                        continue
                                    if len(attribute.value_ids) == 1:
                                        attr.update({'value': attribute.value_ids[0].id})
                                        attr.update({'custom_value': formula[formula_attr]})
                                    else:
                                        for value in attribute.value_ids:
                                            if str(formula[formula_attr]) == value.name:
                                                attr.update({'value': value.id})
                        if 'unite_mm' in formula:
                            print 'quantity unite_mm ', quantity
                            quantity = float(formula['unite_mm']) * quantity
                                    
                else:
                    product = bom_line_id.product_id
                    product_attributes = (
                        bom_line_id.product_id.
                        _get_product_attributes_values_dict())
                # 'product_id': product and product.id,
                if quantity:
                    result.append({
                        'name': (bom_line_id.product_id.name or
                                 bom_line_id.product_template.name),
                        'product_template': (
                            bom_line_id.product_template.id or
                            bom_line_id.product_id.product_tmpl_id.id),
                        'product_qty': quantity,
                        'product_uom': bom_line_id.product_uom.id,
                        'product_uos_qty': (
                            bom_line_id.product_uos and
                            _factor((bom_line_id.product_uos_qty * factor),
                                    bom_line_id.product_efficiency,
                                    bom_line_id.product_rounding) or False),
                        'product_uos': (bom_line_id.product_uos and
                                        bom_line_id.product_uos.id or False),
                        'product_attributes': map(lambda x: (0, 0, x),
                                                  product_attributes),
                    })
            elif bom_id:
                all_prod = [bom.product_tmpl_id.id] + (previous_products or [])
                bom2 = self.browse(bom_id)
                # We need to convert to units/UoM of chosen BoM
                factor2 = uom_obj._compute_qty(
                    bom_line_id.product_uom.id, quantity, bom2.product_uom.id)
                quantity2 = factor2 / bom2.product_qty
                res = self._bom_explode(
                    bom2, bom_line_id.product_id, quantity2,
                    properties=properties, level=level + 10,
                    previous_products=all_prod, master_bom=master_bom,
                    production=production)
                result = result + res[0]
                result2 = result2 + res[1]
            else:
                if not bom_line_id.product_id:
                    name = bom_line_id.product_template.name_get()[0][1]
                else:
                    name = bom_line_id.product_id.name_get()[0][1]
                raise exceptions.Warning(
                    _('Invalid Action! BoM "%s" contains a phantom BoM line'
                      ' but the product "%s" does not have any BoM defined.') %
                    (master_bom.name, name))
        return result, result2
