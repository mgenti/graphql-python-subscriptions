import redis
import gevent
import json
from types import FunctionType
from promise import Promise
from graphql import parse, validate, specified_rules, value_from_ast, execute
from graphql.language.ast import OperationDefinition


class RedisPubsub(object):

    def __init__(self, host='localhost', port=6379, *args, **kwargs):
        redis.connection.socket = gevent.socket
        self.redis = redis.StrictRedis(host, port, *args, **kwargs)
        self.pubsub = self.redis.pubsub(ignore_subscribe_messages=True)
        self.subscriptions = {}
        self.sub_id_counter = 0
        self.greenlet = None

    def publish(self, trigger_name, message):
        self.redis.publish(trigger_name, json.dumps(message))
        return True

    def subscribe(self, trigger_name, on_message_handler, options):
        self.pubsub.subscribe(trigger_name)
        if not self.greenlet:
            self.greenlet = gevent.spawn(
                self.wait_and_get_message,
                on_message_handler
            )
        self.sub_id_counter += 1
        self.subscriptions[self.sub_id_counter] = trigger_name
        return Promise.resolve(self.sub_id_counter)

    def unsubscribe(self, sub_id):
        trigger_name = self.subscriptions[sub_id]
        del self.subscriptions[sub_id]
        self.pubsub.unsubscribe(trigger_name)
        if not self.subscriptions:
            self.greenlet = self.greenlet.kill()

    def wait_and_get_message(self, on_message_handler):
        while True:
            message = self.pubsub.get_message()
            if message:
                on_message_handler(json.loads(message['data']))
            gevent.sleep(.001)  # may not need this sleep call - test


class ValidationError(Exception):

    def __init__(self, errors):
        self.errors = errors
        self.message = 'Subscription query has validation errors'


class SubscriptionManager(object):

    def __init__(self, schema, pubsub, setup_funcs={}):
        self.schema = schema
        self.pubsub = pubsub
        self.setup_funcs = setup_funcs
        self.subscriptions = {}
        self.max_subscription_id = 0

    def publish(self, trigger_name, payload):
        self.pubsub.publish(trigger_name, payload)

    def subscribe(self, query, operation_name, callback, variables, context,
                  format_error, format_response):
        parsed_query = parse(query)
        errors = validate(
            self.schema,
            parsed_query,
            # TODO: Need to create/add subscriptionHasSingleRootField
            # rule from apollo subscription manager package
            rules=specified_rules
        )

        if errors:
            return Promise.reject(ValidationError(errors))

        args = {}

        subscription_name = ''

        for definition in parsed_query.definitions:

            if isinstance(definition, OperationDefinition):
                root_field = definition.selection_set.selections[0]
                subscription_name = root_field.name.value

                fields = self.schema.get_subscription_type().fields

                for arg in root_field.arguments:

                    arg_definition = [
                        arg_def for _, arg_def in
                        fields.get(subscription_name).args.iteritems() if
                        arg_def.out_name == arg.name.value
                    ][0]

                    args[arg_definition.out_name] = value_from_ast(
                        arg.value,
                        arg_definition.type,
                        variables=variables
                    )

        if self.setup_funcs.get(subscription_name):
            trigger_map = self.setup_funcs[subscription_name](
                query,
                operation_name,
                callback,
                variables,
                context,
                format_error,
                format_response,
                args,
                subscription_name
            )
        else:
            trigger_map = {}
            trigger_map[subscription_name] = {}

        external_subscription_id = self.max_subscription_id
        self.max_subscription_id += 1
        self.subscriptions[external_subscription_id] = []
        subscription_promises = []

        for trigger_name in trigger_map.keys():
            channel_options = trigger_map[trigger_name].get(
                'channel_options',
                {}
            )
            filter = trigger_map[trigger_name].get(
                'filter',
                lambda arg1, arg2: True
            )

            def on_message(root_value):

                def context_promise_handler(result):
                    if isinstance(context, FunctionType):
                        return context()
                    else:
                        return context

                def filter_func_promise_handler(context):
                    return Promise.all([
                        context,
                        filter(root_value, context)
                    ])

                def context_do_execute_handler(result):
                    context, do_execute = result
                    if not do_execute:
                        return
                    execute(
                        self.schema,
                        parsed_query,
                        root_value,
                        context,
                        variables,
                        operation_name
                    )

                return Promise.resolve(
                    True
                ).then(
                    context_promise_handler
                ).then(
                    filter_func_promise_handler
                ).then(
                    context_do_execute_handler
                ).then(
                    lambda data: callback(None, data)
                ).catch(
                    lambda error: callback(error)
                )

            subscription_promises.append(
                self.pubsub.subscribe(
                    trigger_name,
                    on_message,
                    channel_options
                ).then(
                    lambda id: self.subscriptions[
                        external_subscription_id].append(id)
                )
            )

        return Promise.all(subscription_promises).then(
            lambda result: external_subscription_id
        )

    def unsubscribe(self, sub_id):
        for internal_id in self.subscriptions.get(sub_id):
            self.pubsub.unsubscribe(internal_id)
        self.subscriptions.pop(sub_id, None)