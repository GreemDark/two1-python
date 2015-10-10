"""Views for Payment Channels."""

import os
from rest_framework import status
from rest_framework.views import APIView
from rest_framework.response import Response

from .utils import PCUtil
from .models import Transaction
from .paymentserver import PaymentServer, PaymentServerError
import two1.examples.server.settings as settings

server = PaymentServer(
    settings.TWO1_WALLET,
    account=os.environ.get('WALLET_ACCOUNT', 'default'),
    testnet=settings.TWO1_TESTNET)


class PaymentAPIError(Exception):
    pass


class BadParametersError(PaymentAPIError):
    pass


class PaymentServerAuthBypass(BaseAuthentication):

    """Authenticates users for the purpose of opening a payment channel.

    Users wanting to open a payment channel shouldn't be routed through the
    'payment required' flow, as they only want to open a channel, not purchase
    any particular resource.
    """

    def authenticate(self, request):
        print("started: PaymentServerAuthBypass")
        """Return an anonymous payment channel user for auth purposes."""
        user, _ = get_user_model().objects.get_or_create(
            username='payment_channel_user')
        return (user, None)


@authentication_classes([PaymentServerAuthBypass])
class Handshake(APIView):

    """View to handle the payment channel handshake.

    The handshake involves two parties: a merchant and a customer, consting of:

    1. Discovery (GET '/')
    2. Initialization (POST '/')
    3. Completion (PUT '/<deposit_transaction_id>')
        Note that the completion URI references a specific resource. This is
        because the handshake is actually completed inside  the channel (see
        the Channel View below).
    """

    def get(self, request, format='json'):
        """Return the merchant's public key to the requester.

        Params (query):
            none

        Response (json) 2xx:
            public_key (string): string representation of merchant's public key
        """
        public_key = server.discovery()
        return Response({'public_key': public_key})

    def post(self, request, format='json'):
        """Initialize a payment channel.

        This endpoint expects to receive a hex-serialized refund transaction
        encoded as a string. It should be a multisignature transaction, half
        of which has already been signed by the customer. This endpoint signs
        and returns the final transaction.

        Params (query):
            refund_tx (string): half-signed serialized refund transaction

        Response (json) 2xx:
            refund_tx (string): fully-signed serialized refund transaction

        Response (json) 4xx:
            error (string): reason for failed request
        """
        params = request.data

        try:
            # Validate parameters
            if 'refund_tx' not in params:
                raise BadParametersError('No refund provided.')

            # Initialize the payment channel
            refund_tx = PCUtil.parse_tx(params['refund_tx'])
            server.initialize_handshake(refund_tx)

            # Respond with the fully-signed refund transaction
            success = {'refund_tx': PCUtil.serialize_tx(refund_tx)}
            return Response(success)
        except (PaymentAPIError, PaymentServerError) as e:
            # Catch payment exceptions and send error response to client
            error = {'error': str(e)}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)


@authentication_classes([PaymentServerAuthBypass])
class Channel(APIView):

    """View to handle the payment channel.

    The channel concerns itself with completing the handshake, receiving
    payments, providing channel status, and closing the status:

    1. Handshake Completion (PUT '/<deposit_transaction_id>')
    2. Status (GET '/<deposit_transaction_id>')
    2. Payment Receipt (PUT '/<deposit_transaction_id>')
    2. Close (DELETE '/<deposit_transaction_id>')
    """

    def get(self, request, deposit_tx_id=None):
        """Get a payment channel's current status.

        Params (query):
            deposit_tx_id (string): initial signed deposit transaction id

        Response (json) 2xx:
            status (object): object containing current channel status

        Response (json) 4xx:
            error (string): reason for failed request
        """
        try:
            info = server.status(deposit_tx_id)
            return Response(info)
        except Transaction.DoesNotExist:
            error = {'error': 'Payment channel not found.'}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def put(self, request, deposit_tx_id=None, format='json'):
        """Finalize a channel or make a payment in payment channel.

        Params (query):
            deposit_tx_id (string): initial signed deposit transaction id

        Params (json) (one of the following):
            deposit_tx (string): half-signed serialized deposit transaction
            payment_tx (string):  half-signed serialized payment transaction

        Response (json) 2xx:
            no body

        Response (json) 4xx:
            error (string): reason for failed request
        """
        params = request.data

        try:
            if 'deposit_tx' in params:
                # Complete the handshake using the received deposit
                deposit_tx = PCUtil.parse_tx(params['deposit_tx'])
                server.complete_handshake(deposit_tx_id, deposit_tx)
                return Response(status=status.HTTP_200_OK)

            elif 'payment_tx' in params:
                # Receive a payment in the channel using the received payment
                payment_tx = PCUtil.parse_tx(params['payment_tx'])
                server.receive_payment(deposit_tx_id, payment_tx)
                return Response({'payment_txid': str(payment_tx.hash)})

            else:
                # We didn't get the right parameters
                raise BadParametersError('Incorrect parameters.')
        except (PaymentAPIError, PaymentServerError) as e:
            error = {'error': str(e)}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)

    def delete(self, request, deposit_tx_id=None):
        """Close a payment channel.

        Params (query):
            deposit_tx_id (string): initial signed deposit transaction id

        Response (json) 2xx:
            no body

        Response (json) 4xx:
            error (string): reason for failed request
        """
        try:
            payment_tx_id = server.close(deposit_tx_id)
            return Response({'payment_txid': payment_tx_id})
        except (Transaction.DoesNotExist, PaymentAPIError,
                PaymentServerError) as e:
            error = {'error': str(e)}
            return Response(error, status=status.HTTP_400_BAD_REQUEST)