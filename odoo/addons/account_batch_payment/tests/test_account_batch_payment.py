# -*- coding: utf-8 -*-
from odoo import Command
from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.tests import tagged
from odoo.exceptions import RedirectWarning, ValidationError

@tagged('post_install', '-at_install')
class TestAccountBatchPayment(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.groups_id |= cls.env.ref('account.group_validate_bank_account')
        cls.other_currency = cls.setup_other_currency('EUR')

        cls.payment_debit_account_id = cls.copy_account(cls.inbound_payment_method_line.payment_account_id)
        cls.payment_credit_account_id = cls.copy_account(cls.outbound_payment_method_line.payment_account_id)

        cls.partner_bank_account = cls.env['res.partner.bank'].create({
            'acc_number': 'BE32707171912447',
            'partner_id': cls.partner_a.id,
            'allow_out_payment': True,
            'acc_type': 'bank',
        })

    def _create_multi_company_payments_and_context(self, companies_dict, add_company_context=None):
        payments = self.env['account.payment']
        companies_context = self.env['res.company']
        field_record = self.env['ir.model.fields']._get('res.partner', 'property_account_receivable_id')
        property_account_receivable = self.env['ir.default'].search(
            [('field_id', '=', field_record.id), ('company_id', '=', self.company_data['company'].id)], limit=1
        )

        for company, create_property_account_receivable in companies_dict.items():
            if create_property_account_receivable:
                # needed for computation of payment.destination_account_id
                property_account_receivable.copy({'company_id': company.id})
            payment = self.env['account.payment'].with_company(company).create({
                'amount': 100.0,
                'payment_type': 'inbound',
                'partner_type': 'customer',
                'partner_id': self.partner_a.id,
            })
            payment.action_post()
            payments += payment
            companies_context += company

        if add_company_context:
            companies_context += add_company_context

        context = {
            **self.env.context,
            'allowed_company_ids': companies_context.ids,
            'active_ids': payments.ids,
            'active_model': 'account.payment',
        }

        return payments, context

    def test_create_batch_payment_from_payment(self):
        payments = self.env['account.payment']
        for dummy in range(2):
            payments += self.env['account.payment'].create({
                'amount': 100.0,
                'payment_type': 'outbound',
                'partner_type': 'supplier',
                'partner_id': self.partner_a.id,
                'destination_account_id': self.partner_a.property_account_payable_id.id,
                'currency_id': self.other_currency.id,
                'partner_bank_id': self.partner_bank_account.id,
            })

        payments.action_post()
        batch_payment_action = payments.create_batch_payment()
        batch_payment_id = self.env['account.batch.payment'].browse(batch_payment_action.get('res_id'))
        self.assertEqual(len(batch_payment_id.payment_ids), 2)

    def test_change_payment_state(self):
        """
        Check if the amount is well computed when we change a payment state
        """
        payments = self.env['account.payment']
        for _ in range(2):
            payments += self.env['account.payment'].create({
                'amount': 100.0,
                'payment_type': 'inbound',
                'partner_type': 'supplier',
                'partner_id': self.partner_a.id,
                'destination_account_id': self.partner_a.property_account_payable_id.id,
                'partner_bank_id': self.partner_bank_account.id,
            })
        payments.action_post()

        batch_payment = self.env['account.batch.payment'].create(
            {
                'journal_id': payments.journal_id.id,
                'payment_method_id': payments.payment_method_id.id,
                'payment_ids': [
                    (6, 0, payments.ids)
                ],
            }
        )

        self.assertEqual(batch_payment.amount, 200)

        payments[0].move_id.button_draft()

        # Check that we still keep it
        self.assertEqual(batch_payment.amount, 200)

    def test_validate_batch(self):
        """
        Check that we can only validate a batch if all the payments are in progress
        """
        payments = self.env['account.payment']
        for _ in range(2):
            payments += self.env['account.payment'].create({
                'amount': 100.0,
                'payment_type': 'inbound',
                'partner_type': 'supplier',
                'partner_id': self.partner_a.id,
                'destination_account_id': self.partner_a.property_account_payable_id.id,
                'partner_bank_id': self.partner_bank_account.id,
            })
        payments.action_post()

        batch_payment = self.env['account.batch.payment'].create(
            {
                'journal_id': payments.journal_id.id,
                'payment_method_id': payments.payment_method_id.id,
                'payment_ids': [Command.set(payments.ids)]
            }
        )
        payments[0].action_validate()

        # In accounting, we can only validate a batch if all the payments are in process
        # While in enterprise invoicing, we can validate a batch even if some payments are paid
        is_accounting_installed = self.env['account.move']._get_invoice_in_payment_state() == 'in_payment'
        if is_accounting_installed:
            with self.assertRaisesRegex(RedirectWarning, "To validate the batch, payments must be in process"):
                batch_payment.validate_batch()
            # Set payment to in process state
            payments[0].action_draft()
            payments[0].action_post()
        action = batch_payment.validate_batch()
        self.assertFalse(action)

    def test_batch_payment_sub_company(self):
        """Test the creation of a batch payment from a sub company"""
        self.company_data['company'].write({'child_ids': [Command.create({'name': 'Good Company'})]})
        child_comp = self.company_data['company'].child_ids[0]

        payment, context = self._create_multi_company_payments_and_context({child_comp: True})

        batch = self.env['account.batch.payment'].with_context(context).create({
            'journal_id': payment.journal_id.id,
        })
        self.assertTrue(batch)

    def test_batch_payment_branches(self):
        """
        Test the creation of a batch payment with branches. When all payments are branches of
        a common head office, a batch payment should be allowed to be created.
        """
        main_company = self.company_data['company']
        main_company.vat = '123'
        branch_1 = self.env['res.company'].create({'name': "Branch 1", 'parent_id': main_company.id})
        branch_2 = self.env['res.company'].create({'name': "Branch 2", 'parent_id': main_company.id, 'vat': '456'})
        branch_2_1 = self.env['res.company'].create({'name': "Branch 2 sub-branch 1", 'parent_id': branch_2.id})

        payments, context = self._create_multi_company_payments_and_context({branch_1: True, branch_2: True, branch_2_1: True})

        batch = self.env['account.batch.payment'].with_context(context).create({
            'journal_id': payments[0].journal_id.id,
            'payment_ids': payments.ids,
        })
        self.assertTrue(batch)

    def test_batch_payment_different_companies(self):
        """ Payments from different companies not belonging to the same head company should raise an error. """
        main_company = self.company_data['company']
        company_b = self.setup_other_company()['company']

        payments, context = self._create_multi_company_payments_and_context({main_company: False, company_b: False}, main_company)

        with self.assertRaisesRegex(ValidationError, "The journal of the batch payment and of the payments it contains must be the same."):
            self.env['account.batch.payment'].with_context(context).create({
                'journal_id': payments[0].journal_id.id,
                'payment_ids': payments.ids,
            })

    def test_batch_payment_foreign_currency(self):
        """
        Make sure that payments in foreign currency are converted for the total amount to be displayed
            currency rate = 1$:10€
            amount_company_currency = 100$
            amount_foreign_currency = 100€ -> 10$
            => batch.amount = 110$
        """
        payments = self.env['account.payment']
        company_currency = self.env.company.currency_id
        foreign_currency = self.other_currency

        self.env['res.currency.rate'].create({
            'name': '2024-05-14',
            'rate': 10,
            'currency_id': foreign_currency.id,
            'company_id': self.env.company.id,
        })

        for currency in (company_currency, foreign_currency):
            payments += self.env['account.payment'].create({
                'amount': 100.0,
                'payment_type': 'inbound',
                'partner_type': 'supplier',
                'partner_id': self.partner_a.id,
                'currency_id': currency.id,
                'date': '2024-05-14',
            })

        payments.action_post()
        batch_payment_action = payments.create_batch_payment()
        batch_payment = self.env['account.batch.payment'].browse(batch_payment_action.get('res_id'))
        self.assertEqual(batch_payment.amount, 110)
        self.assertEqual(batch_payment.amount_residual, 110)
        self.assertEqual(batch_payment.amount_residual_currency, 110)

    def test_batch_payment_journal_foreign_currency(self):
        """
        Test that, if a bank journal is set in a foreign currency, the batch payment will be correctly converted
        currency rate = 1$:10€
        payment of 100€ -> 100☺
        payment of 100$ -> 1000☺
        Total -> 1100
        """
        payments = self.env['account.payment']
        company_currency = self.env.company.currency_id
        foreign_currency = self.other_currency

        self.env['res.currency.rate'].create({
            'name': '2024-05-14',
            'rate': 10,
            'currency_id': foreign_currency.id,
            'company_id': self.env.company.id,
        })
        bank_journal_foreign = self.env['account.journal'].create({
            'name': 'Bank2',
            'type': 'bank',
            'code': 'BNK2',
            'currency_id': foreign_currency.id,
        })

        for currency in (company_currency, foreign_currency):
            payments += self.env['account.payment'].create({
                'amount': 100.0,
                'payment_type': 'inbound',
                'partner_type': 'supplier',
                'partner_id': self.partner_a.id,
                'currency_id': currency.id,
                'date': '2024-05-14',
                'journal_id': bank_journal_foreign.id
            })

        payments.action_post()
        batch_payment_action = payments.create_batch_payment()
        batch_payment = self.env['account.batch.payment'].browse(batch_payment_action.get('res_id'))
        self.assertEqual(batch_payment.amount, 1100)
        self.assertEqual(batch_payment.amount_residual, 110)
        self.assertEqual(batch_payment.amount_residual_currency, 1100)

    def test_create_batch_from_payment_already_in_batch(self):
        payment = self.env['account.payment'].create({
            'amount': 100.0,
            'payment_type': 'outbound',
            'partner_type': 'supplier',
            'partner_id': self.partner_a.id,
            'destination_account_id': self.partner_a.property_account_payable_id.id,
            'currency_id': self.other_currency.id,
            'partner_bank_id': self.partner_bank_account.id,
        })
        payment.action_post()
        batch_payment_action = payment.create_batch_payment()
        batch_payment_id = self.env['account.batch.payment'].browse(batch_payment_action.get('res_id'))
        batch_payment_id.validate_batch()
        with self.assertRaises(ValidationError):
            payment.create_batch_payment()

    def test_amount_in_paid_state(self):
        """
            Verify that the batch payment amount is correctly computed when the payment state is 'paid'.
        """
        payments = self.env['account.payment']

        # Create two inbound payments of 100€ each
        for _ in range(2):
            payments += self.env['account.payment'].create({
                'amount': 100.0,
                'payment_type': 'inbound',
                'partner_type': 'supplier',
                'partner_id': self.partner_a.id,
                'destination_account_id': self.partner_a.property_account_payable_id.id,
                'partner_bank_id': self.partner_bank_account.id,
            })
        # Post the payments to validate them
        payments.action_post()

         # Update payment states to 'paid'
        payments.write({'state': 'paid'})

        # Ensure all payments are now in the 'paid' state
        self.assertTrue(all(payment.state == 'paid' for payment in payments), "Payments should be in 'paid' state")

        # Create a batch payment including the two payments
        batch_payment = self.env['account.batch.payment'].create(
            {
                'journal_id': payments.journal_id.id,
                'payment_method_id': payments.payment_method_id.id,
                'payment_ids': [Command.set(payments.ids)],
            }
        )

        # Ensure the amount remains correct after recomputation
        # When accountant is not installed, payments with paid state
        # should be counted when recomputing amount_residual 
        if self.env['ir.module.module']._get('accountant').state == 'installed':
            self.assertEqual(batch_payment.amount_residual, 0)
            self.assertEqual(batch_payment.amount_residual_currency, 0)
        else:
            self.assertEqual(batch_payment.amount_residual, 200)
            self.assertEqual(batch_payment.amount_residual_currency, 200)
        self.assertEqual(batch_payment.amount, 200)

