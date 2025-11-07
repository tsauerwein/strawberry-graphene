from typing import Type, Optional, Sequence, Union, Any
from decimal import Decimal

import strawberry
from strawberry.extensions import SchemaExtension
from strawberry.types.base import TypeDefinition, StrawberryObjectDefinition
from strawberry.types.union import StrawberryUnion
from strawberry.types.scalar import ScalarDefinition
from strawberry.types.enum import EnumDefinition
from strawberry.types import get_object_definition, has_object_definition
from strawberry.types.field import StrawberryField
from strawberry.schema.config import StrawberryConfig

import graphene
from graphene.types.schema import TypeMap as BaseGrapheneTypeMap

from graphql import (
    ExecutionContext as GraphQLExecutionContext,
    GraphQLSchema,
    validate_schema,
    GraphQLObjectType,
    GraphQLType,
)
from graphql.type.directives import specified_directives


class GraphQLCoreConverter(strawberry.schema.schema_converter.GraphQLCoreConverter):
    def __init__(
        self,
        config: StrawberryConfig,
        scalar_overrides: dict,
        get_fields_func: Any,
    ):
        super().__init__(config, scalar_overrides, get_fields_func)
        self.type_map: GrapheneTypeMap = GrapheneTypeMap(self)

    def add_graphene_type(self, type_: Any) -> GraphQLObjectType:
        return self.type_map.add_type(type_)

    def from_object_type(self, object_type: Type) -> GraphQLObjectType:
        """Convert object type (Strawberry or Graphene) to GraphQLObjectType"""
        # Check if it's a Graphene type
        try:
            if issubclass(object_type, graphene.ObjectType):
                return self.add_graphene_type(object_type)
        except TypeError:
            pass

        # Handle Strawberry types
        if has_object_definition(object_type):
            definition = get_object_definition(object_type)
            return self.from_object(definition)

        raise TypeError(f"Unable to convert {object_type} to GraphQL type")

    def from_type(self, type_: Any) -> GraphQLType:
        """Override to handle graphene types in strawberry type annotations"""
        # Check if it's a Graphene type
        try:
            if isinstance(type_, type) and issubclass(type_, graphene.ObjectType):
                return self.add_graphene_type(type_)
        except TypeError:
            pass

        # Fall back to strawberry's converter
        return super().from_type(type_)

    def from_maybe_optional(self, type_: Any) -> GraphQLType:
        """Override to handle graphene types wrapped in Optional/NonNull"""
        from graphql import GraphQLNonNull
        from typing import get_origin, get_args

        # Check if it's wrapped in Optional
        origin = get_origin(type_)
        is_optional = origin is Union

        if is_optional:
            # Extract the actual type from Optional[T] (which is Union[T, None])
            args = get_args(type_)
            actual_types = [t for t in args if t is not type(None)]
            if actual_types:
                type_ = actual_types[0]

        # Check if it's a Graphene type
        try:
            if isinstance(type_, type) and issubclass(type_, graphene.ObjectType):
                graphene_graphql_type = self.add_graphene_type(type_)
                # Wrap in NonNull if not optional
                if not is_optional:
                    return GraphQLNonNull(graphene_graphql_type)
                return graphene_graphql_type
        except TypeError:
            pass

        # Fall back to strawberry's converter
        result = super().from_maybe_optional(type_)

        # Unwrap strawberry ConcreteType to get the actual GraphQL type
        if hasattr(result, 'implementation'):
            return result.implementation
        return result


class GrapheneTypeMap(BaseGrapheneTypeMap):
    def __init__(self, strawberry_convertor, *args, **kwargs):
        self.strawberry_convertor = strawberry_convertor
        super().__init__(*args, **kwargs)

    def add_type(self, graphene_type):
        # Unwrap strawberry's ConcreteType wrapper if present
        if hasattr(graphene_type, 'implementation'):
            return graphene_type.implementation

        # Check if it's a Strawberry type (has __strawberry_definition__ or _type_definition)
        if has_object_definition(graphene_type):
            definition = get_object_definition(graphene_type)
            return self.strawberry_convertor.from_object(definition)

        # Check for enum definition
        if hasattr(graphene_type, "_enum_definition") or hasattr(graphene_type, "__strawberry_definition__"):
            try:
                return self.strawberry_convertor.from_enum(graphene_type.__strawberry_definition__)
            except (AttributeError, TypeError):
                pass

        # Special case decimal - unwrap to get the implementation
        if isinstance(graphene_type, type) and issubclass(
            graphene_type, graphene.Decimal
        ):
            result = self.strawberry_convertor.from_scalar(Decimal)
            if hasattr(result, 'implementation'):
                return result.implementation
            return result

        # Call parent and unwrap any ConcreteType results
        result = super().add_type(graphene_type)
        if hasattr(result, 'implementation'):
            return result.implementation
        return result


class Schema(strawberry.Schema):
    def __init__(
        self,
        # TODO: can we make sure we only allow to pass something that has been decorated?
        query: Type,
        mutation: Optional[Type] = None,
        subscription: Optional[Type] = None,
        directives=(),
        types=(),
        extensions: Sequence[Type[SchemaExtension]] = (),
        execution_context_class: Optional[Type[GraphQLExecutionContext]] = None,
    ):
        self.extensions = extensions
        self.execution_context_class = execution_context_class
        self.config = StrawberryConfig()
        self.directives = directives
        self.schema_directives = []
        self._cached_middleware_manager = None

        # Create converter with required parameters
        self.schema_converter = GraphQLCoreConverter(
            config=self.config,
            scalar_overrides={},
            get_fields_func=self.get_fields,
        )

        query_type = self.schema_converter.from_object_type(query)
        mutation_type = (
            self.schema_converter.from_object_type(mutation) if mutation else None
        )
        subscription_type = (
            self.schema_converter.from_object_type(subscription)
            if subscription
            else None
        )

        graphql_directives = [
            self.schema_converter.from_directive(directive.directive_definition)
            for directive in directives
        ]

        self._schema = GraphQLSchema(
            query=query_type,
            mutation=mutation_type,
            subscription=subscription_type if subscription else None,
            directives=specified_directives + tuple(graphql_directives),
            types=list(map(self.schema_converter.from_object_type, types)),
        )

        # Validate schema early because we want developers to know about
        # possible issues as soon as possible
        errors = validate_schema(self._schema)
        if errors:
            formatted_errors = "\n\n".join(f"❌ {error.message}" for error in errors)
            raise ValueError(f"Invalid Schema. Errors:\n\n{formatted_errors}")

        self.query = self.schema_converter.type_map[query_type.name]

    def __str__(self) -> str:
        """Override to use internal _schema for printing"""
        from graphql import print_schema as graphql_print_schema
        return graphql_print_schema(self._schema)

    def get_fields(
        self, type_definition: StrawberryObjectDefinition
    ) -> list[StrawberryField]:
        """Get fields from a type definition"""
        return type_definition.fields

    def get_type_by_name(
        self, name: str
    ) -> Optional[
        Union[TypeDefinition, ScalarDefinition, EnumDefinition, StrawberryUnion]
    ]:
        if name in self.schema_converter.type_map:
            return getattr(self.schema_converter.type_map[name], "definition", None)

        return None
