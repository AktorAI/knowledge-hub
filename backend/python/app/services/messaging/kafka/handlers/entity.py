from typing import Optional
from uuid import uuid4

from app.config.constants.arangodb import (
    AccountType,
    CollectionNames,
    Connectors,
    ConnectorScopes,
)
from app.connectors.core.base.event_service.event_service import BaseEventService
from app.connectors.core.factory.connector_factory import ConnectorFactory
from app.connectors.services.base_arango_service import (
    BaseArangoService as ArangoService,
)
from app.containers.connector import (
    ConnectorAppContainer,
)
from app.utils.time_conversion import get_epoch_timestamp_in_ms

# Constants for the Global Reader team
GLOBAL_READER_TEAM_NAME = "Global Reader"
READER_ROLE = "READER"
OWNER_ROLE = "OWNER"


class EntityEventService(BaseEventService):
    def __init__(self, logger,
                arango_service: ArangoService,
                app_container: ConnectorAppContainer) -> None:
        self.logger = logger
        self.arango_service = arango_service
        self.app_container = app_container

    async def process_event(self, event_type: str, payload: dict) -> bool:
        """Handle entity-related events by calling appropriate handlers"""
        try:
            self.logger.info(f"Processing entity event: {event_type}")
            if event_type == "orgCreated":
                return await self.__handle_org_created(payload)
            elif event_type == "orgUpdated":
                return await self.__handle_org_updated(payload)
            elif event_type == "orgDeleted":
                return await self.__handle_org_deleted(payload)
            elif event_type == "userAdded":
                return await self.__handle_user_added(payload)
            elif event_type == "userUpdated":
                return await self.__handle_user_updated(payload)
            elif event_type == "userDeleted":
                return await self.__handle_user_deleted(payload)
            elif event_type == "appEnabled":
                return await self.__handle_app_enabled(payload)
            elif event_type == "appDisabled":
                return await self.__handle_app_disabled(payload)
            elif event_type == "userLoggedIn":
                return await self.__handle_user_logged_in(payload)
            else:
                self.logger.error(f"Unknown entity event type: {event_type}")
                return False
        except Exception as e:
            self.logger.error(f"Error processing entity event: {str(e)}")
            return False

    async def __handle_sync_event(self,event_type: str, value: dict) -> bool:
        """Handle sync-related events by sending them to the sync-events topic"""
        try:
            # Prepare the message
            message = {
                'eventType': event_type,
                'payload': value,
                'timestamp': get_epoch_timestamp_in_ms()
            }

            # Send the message to sync-events topic using aiokafka
            await self.app_container.messaging_producer.send_message(
                topic='sync-events',
                message=message
            )

            self.logger.info(f"Successfully sent sync event: {event_type}")
            return True

        except Exception as e:
            self.logger.error(f"Error sending sync event: {str(e)}")
            return False

    # ORG EVENTS
    async def __handle_org_created(self, payload: dict) -> bool:
        """Handle organization creation event"""

        accountType = (
            AccountType.ENTERPRISE.value
            if payload["accountType"] in [AccountType.BUSINESS.value, AccountType.ENTERPRISE.value]
            else AccountType.INDIVIDUAL.value
        )
        try:
            org_data = {
                "_key": payload["orgId"],
                "name": payload.get("registeredName", "Individual Account"),
                "accountType": accountType,
                "isActive": True,
                "createdAtTimestamp": get_epoch_timestamp_in_ms(),
                "updatedAtTimestamp": get_epoch_timestamp_in_ms(),
            }

            # Batch upsert org
            await self.arango_service.batch_upsert_nodes(
                [org_data], CollectionNames.ORGS.value
            )

            # Write a query to get departments with orgId == None
            query = f"""
                FOR d IN {CollectionNames.DEPARTMENTS.value}
                FILTER d.orgId == null
                RETURN d
            """
            cursor = self.arango_service.db.aql.execute(query) # type: ignore
            departments = list(cursor) # type: ignore

            # Create relationships between org and departments
            org_department_relations = []
            for department in departments:
                relation_data = {
                    "_from": f"{CollectionNames.ORGS.value}/{payload['orgId']}",
                    "_to": f"{CollectionNames.DEPARTMENTS.value}/{department['_key']}",
                    "createdAtTimestamp": get_epoch_timestamp_in_ms(),
                }
                org_department_relations.append(relation_data)

            if org_department_relations:
                await self.arango_service.batch_create_edges(
                    org_department_relations,
                    CollectionNames.ORG_DEPARTMENT_RELATION.value,
                )
                self.logger.info(
                    f"✅ Successfully created organization: {payload['orgId']} and relationships with departments"
                )
            else:
                self.logger.info(
                    f"✅ Successfully created organization: {payload['orgId']}"
                )

            # Automatically create Knowledge Base connector instance for the new org
            await self.__create_kb_connector_app_instance(payload['orgId'], payload.get('userId'))

            return True

        except Exception as e:
            self.logger.error(f"❌ Error creating organization: {str(e)}")
            return False

    async def __handle_org_updated(self, payload: dict) -> bool:
        """Handle organization update event"""
        try:
            self.logger.info(f"📥 Processing org updated event: {payload}")
            org_data = {
                "_key": payload["orgId"],
                "name": payload["registeredName"],
                "updatedAtTimestamp": get_epoch_timestamp_in_ms(),
            }

            # Batch upsert org
            await self.arango_service.batch_upsert_nodes(
                [org_data], CollectionNames.ORGS.value
            )
            self.logger.info(
                f"✅ Successfully updated organization: {payload['orgId']}"
            )
            return True

        except Exception as e:
            self.logger.error(f"❌ Error updating organization: {str(e)}")
            return False

    async def __handle_org_deleted(self, payload: dict) -> bool:
        """Handle organization deletion event"""
        try:
            self.logger.info(f"📥 Processing org deleted event: {payload}")
            org_data = {
                "_key": payload["orgId"],
                "isActive": False,
                "updatedAtTimestamp": get_epoch_timestamp_in_ms(),
            }

            # Batch upsert org with isActive = False
            await self.arango_service.batch_upsert_nodes(
                [org_data], CollectionNames.ORGS.value
            )
            self.logger.info(
                f"✅ Successfully soft-deleted organization: {payload['orgId']}"
            )
            return True

        except Exception as e:
            self.logger.error(f"❌ Error deleting organization: {str(e)}")
            return False

    # USER EVENTS
    async def __handle_user_added(self, payload: dict) -> bool:
        """Handle user creation event"""
        try:
            self.logger.info(f"📥 Processing user added event: {payload}")
            # Check if user already exists by email
            existing_user = await self.arango_service.get_user_by_email(
                payload["email"]
            )

            current_timestamp = get_epoch_timestamp_in_ms()

            if existing_user:
                user_key = existing_user.id
                user_data = {
                    "_key": user_key,
                    "userId": payload["userId"],
                    "orgId": payload["orgId"],
                    "isActive": True,
                    "updatedAtTimestamp": current_timestamp,
                }
            else:
                user_key = str(uuid4())
                user_data = {
                    "_key": user_key,
                    "userId": payload["userId"],
                    "orgId": payload["orgId"],
                    "email": payload["email"],
                    "fullName": payload.get("fullName", ""),
                    "firstName": payload.get("firstName", ""),
                    "middleName": payload.get("middleName", ""),
                    "lastName": payload.get("lastName", ""),
                    "designation": payload.get("designation", ""),
                    "businessPhones": payload.get("businessPhones", []),
                    "isActive": True,
                    "createdAtTimestamp": current_timestamp,
                    "updatedAtTimestamp": current_timestamp,
                }

            # Get org details to check account type
            org_id = payload["orgId"]
            org = await self.arango_service.get_document(
                org_id, CollectionNames.ORGS.value
            )
            if not org:
                self.logger.error(f"Organization not found: {org_id}")
                return False

            # Batch upsert user
            await self.arango_service.batch_upsert_nodes(
                [user_data], CollectionNames.USERS.value
            )

            # Create edge between org and user if it doesn't exist
            edge_data = {
                "_to": f"{CollectionNames.ORGS.value}/{payload['orgId']}",
                "_from": f"{CollectionNames.USERS.value}/{user_data['_key']}",
                "entityType": "ORGANIZATION",
                "createdAtTimestamp": current_timestamp,
            }
            await self.arango_service.batch_create_edges(
                [edge_data],
                CollectionNames.BELONGS_TO.value,
            )

            # Get or create knowledge base for the user
            await self.__get_or_create_knowledge_base(user_key,payload["userId"], payload["orgId"])

            # Create user-app relation edge for KB app
            await self.__create_user_kb_app_relation(user_key, payload["orgId"])

            # Only proceed with app connections if syncAction is 'immediate'
            if payload["syncAction"] == "immediate":
                # Get all apps associated with the org
                org_apps = await self.arango_service.get_org_apps(payload["orgId"])

                for app in org_apps:
                    if app["name"].lower() in ["calendar"]:
                        self.logger.info("Skipping init")
                        continue

                    # Start sync for the specific user
                    await self.__handle_sync_event(
                        event_type=f'{app["name"].lower()}.user',
                        value={
                            "email": payload["email"],
                            "connector":app["name"]
                        },
                    )

            # Add user to Global Reader team (non-blocking)
            is_admin = payload.get("isAdmin", False)
            await self.__add_user_to_global_reader_team(
                user_key=user_data["_key"],
                org_id=payload["orgId"],
                is_admin=is_admin,
            )

            self.logger.info(
                f"✅ Successfully created/updated user: {payload['email']}"
            )
            return True

        except Exception as e:
            self.logger.error(f"❌ Error creating/updating user: {str(e)}")
            return False

    async def __add_user_to_global_reader_team(
        self,
        user_key: str,
        org_id: str,
        is_admin: bool = False,
    ) -> bool:
        """
        Add user to the Global Reader team with appropriate role.
        Admin users get OWNER role, regular users get READER role.
        This is non-blocking - errors are logged but don't fail user creation.
        """
        try:
            # Find the Global Reader team for this org
            query = f"""
            FOR team IN {CollectionNames.TEAMS.value}
                FILTER team.orgId == @orgId AND team.name == @teamName
                RETURN team
            """
            bind_vars = {
                "orgId": org_id,
                "teamName": GLOBAL_READER_TEAM_NAME,
            }
            cursor = self.arango_service.db.aql.execute(query, bind_vars=bind_vars)
            teams = list(cursor)

            if not teams:
                self.logger.warn(
                    f"Global Reader team not found for org {org_id}, skipping user addition"
                )
                return False

            team = teams[0]
            team_key = team["_key"]
            role = OWNER_ROLE if is_admin else READER_ROLE

            # Create permission edge from user to team (UPSERT prevents duplicates)
            current_timestamp = get_epoch_timestamp_in_ms()
            permission_edge = {
                "_from": f"{CollectionNames.USERS.value}/{user_key}",
                "_to": f"{CollectionNames.TEAMS.value}/{team_key}",
                "type": "USER",
                "role": role,
                "createdAtTimestamp": current_timestamp,
                "updatedAtTimestamp": current_timestamp,
            }

            await self.arango_service.batch_create_edges(
                [permission_edge],
                CollectionNames.PERMISSION.value,
            )

            self.logger.info(
                f"✅ Added user {user_key} to Global Reader team with role {role}"
            )
            return True

        except Exception as e:
            # Non-blocking: log error but don't fail user creation
            self.logger.error(
                f"❌ Failed to add user to Global Reader team: {str(e)}"
            )
            return False

    async def __handle_user_logged_in(self, payload: dict) -> bool:
        """
        Handle user login event for Global Reader team membership sync.
        This handles:
        1. Existing users not in team - add them
        2. Admin promotions - upgrade READER to OWNER
        3. Admin demotions - downgrade OWNER to READER
        """
        try:
            self.logger.info(f"📥 Processing user logged in event: {payload}")

            org_id = payload.get("orgId")
            user_id = payload.get("userId")
            is_admin = payload.get("isAdmin", False)

            if not org_id or not user_id:
                self.logger.error("Missing orgId or userId in userLoggedIn event")
                return False

            # Get user from ArangoDB
            user = await self.arango_service.get_user_by_user_id(user_id)
            if not user:
                self.logger.warn(f"User {user_id} not found in ArangoDB, skipping Global Reader sync")
                return False

            user_key = user["_key"]

            # Find the Global Reader team
            query = f"""
            FOR team IN {CollectionNames.TEAMS.value}
                FILTER team.orgId == @orgId AND team.name == @teamName
                RETURN team
            """
            bind_vars = {
                "orgId": org_id,
                "teamName": GLOBAL_READER_TEAM_NAME,
            }
            cursor = self.arango_service.db.aql.execute(query, bind_vars=bind_vars)
            teams = list(cursor)

            if not teams:
                self.logger.warn(
                    f"Global Reader team not found for org {org_id}, skipping membership sync"
                )
                return False

            team = teams[0]
            team_key = team["_key"]
            expected_role = OWNER_ROLE if is_admin else READER_ROLE

            # Check if user is already in team
            permission_query = f"""
            FOR perm IN {CollectionNames.PERMISSION.value}
                FILTER perm._from == @userVertex AND perm._to == @teamVertex
                RETURN perm
            """
            perm_bind_vars = {
                "userVertex": f"{CollectionNames.USERS.value}/{user_key}",
                "teamVertex": f"{CollectionNames.TEAMS.value}/{team_key}",
            }
            perm_cursor = self.arango_service.db.aql.execute(permission_query, bind_vars=perm_bind_vars)
            existing_perms = list(perm_cursor)

            current_timestamp = get_epoch_timestamp_in_ms()

            if existing_perms:
                # User is in team - check if role needs updating
                current_perm = existing_perms[0]
                current_role = current_perm.get("role")

                if current_role != expected_role:
                    # Update the role
                    update_query = f"""
                    UPDATE @key WITH {{ role: @role, updatedAtTimestamp: @timestamp }}
                    IN {CollectionNames.PERMISSION.value}
                    """
                    self.arango_service.db.aql.execute(
                        update_query,
                        bind_vars={
                            "key": current_perm["_key"],
                            "role": expected_role,
                            "timestamp": current_timestamp,
                        }
                    )
                    self.logger.info(
                        f"✅ Updated user {user_key} role in Global Reader team: {current_role} → {expected_role}"
                    )
                else:
                    self.logger.debug(
                        f"User {user_key} already has correct role {current_role} in Global Reader team"
                    )
            else:
                # User not in team - add them
                permission_edge = {
                    "_from": f"{CollectionNames.USERS.value}/{user_key}",
                    "_to": f"{CollectionNames.TEAMS.value}/{team_key}",
                    "type": "USER",
                    "role": expected_role,
                    "createdAtTimestamp": current_timestamp,
                    "updatedAtTimestamp": current_timestamp,
                }

                await self.arango_service.batch_create_edges(
                    [permission_edge],
                    CollectionNames.PERMISSION.value,
                )
                self.logger.info(
                    f"✅ Added existing user {user_key} to Global Reader team with role {expected_role}"
                )

            return True

        except Exception as e:
            # Non-blocking: log error but don't fail login
            self.logger.error(
                f"❌ Failed to sync Global Reader team membership on login: {str(e)}"
            )
            return False

    async def __handle_user_updated(self, payload: dict) -> bool:
        """Handle user update event"""
        try:
            self.logger.info(f"📥 Processing user updated event: {payload}")
            # Find existing user by email
            existing_user = await self.arango_service.get_user_by_user_id(
                payload["userId"],
            )

            if not existing_user:
                self.logger.error(f"User not found with userId: {payload['userId']}")
                return False
            user_data = {
                "_key": existing_user["_key"],
                "userId": payload["userId"],
                "orgId": payload["orgId"],
                "email": payload["email"],
                "fullName": payload.get("fullName", ""),
                "firstName": payload.get("firstName", ""),
                "middleName": payload.get("middleName", ""),
                "lastName": payload.get("lastName", ""),
                "designation": payload.get("designation", ""),
                "businessPhones": payload.get("businessPhones", []),

                "isActive": True,
                "updatedAtTimestamp": get_epoch_timestamp_in_ms(),
            }

            # Add only non-null optional fields
            optional_fields = [
                "fullName",
                "firstName",
                "middleName",
                "lastName",
                "email",
            ]
            user_data.update(
                {
                    key: payload[key]
                    for key in optional_fields
                    if payload.get(key) is not None
                }
            )

            # Batch upsert user
            await self.arango_service.batch_upsert_nodes(
                [user_data], CollectionNames.USERS.value
            )
            self.logger.info(f"✅ Successfully updated user: {payload['email']}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Error updating user: {str(e)}")
            return False

    async def __handle_user_deleted(self, payload: dict) -> bool:
        """Handle user deletion event"""
        try:
            self.logger.info(f"📥 Processing user deleted event: {payload}")
            # Find existing user by userId
            existing_user = await self.arango_service.get_entity_id_by_email(
                payload["email"]
            )
            if not existing_user:
                self.logger.error(f"User not found with mail: {payload['email']}")
                return False

            user_data = {
                "_key": existing_user,
                "orgId": payload["orgId"],
                "email": payload["email"],
                "isActive": False,
                "updatedAtTimestamp": get_epoch_timestamp_in_ms(),
            }

            # Batch upsert user with isActive = False
            await self.arango_service.batch_upsert_nodes(
                [user_data], CollectionNames.USERS.value
            )
            self.logger.info(f"✅ Successfully soft-deleted user: {payload['email']}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Error deleting user: {str(e)}")
            return False

    # APP EVENTS
    async def __handle_app_enabled(self, payload: dict) -> bool:
        """Handle app enabled event"""
        try:
            self.logger.info(f"📥 Processing app enabled event: {payload}")
            org_id = payload["orgId"]
            apps = payload["apps"]
            sync_action = payload.get("syncAction", "none")
            connector_id = payload.get("connectorId", "")
            scope = payload.get("scope", ConnectorScopes.PERSONAL.value)
            # Get org details to check account type
            org = await self.arango_service.get_document(
                org_id, CollectionNames.ORGS.value
            )
            if not org:
                self.logger.error(f"Organization not found: {org_id}")
                return False

            for app_name in apps:
                if sync_action == "immediate":
                    # Start sync for each app (connector already initialized for standard connectors)
                    await self.__handle_sync_event(
                        event_type=f"{app_name.lower()}.start",
                        value={
                            "orgId": org_id,
                            "connector":app_name,
                            "connectorId":connector_id,
                            "scope": scope,
                        },
                    )

            self.logger.info(f"✅ Successfully enabled apps for org: {org_id}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Error enabling apps: {str(e)}")
            return False

    async def __handle_app_disabled(self, payload: dict) -> bool:
        """Handle app disabled event"""
        try:
            org_id = payload["orgId"]
            apps = payload["apps"]
            connector_id = payload.get("connectorId", "")

            if not org_id or not apps:
                self.logger.error("Both orgId and apps are required to disable apps")
                return False

            # Stop sync for each app
            self.logger.info(f"📥 Processing app disabled event: {payload}")

            # Set apps as inactive
            app_updates = []
            for app_name in apps:
                app_doc = await self.arango_service.get_document(
                    connector_id, CollectionNames.APPS.value
                )
                if not app_doc:
                    self.logger.error(f"App not found: {app_name}")
                    return False
                app_data = {
                    "_key": connector_id,  # Construct the app _key
                    "name": app_doc["name"],
                    "type": app_doc["type"],
                    "appGroup": app_doc["appGroup"],
                    "isActive": False,
                    "createdAtTimestamp": app_doc["createdAtTimestamp"],
                    "updatedAtTimestamp": get_epoch_timestamp_in_ms(),
                }
                app_updates.append(app_data)

            # Update apps in database
            await self.arango_service.batch_upsert_nodes(
                app_updates, CollectionNames.APPS.value
            )

            self.logger.info(f"✅ Successfully disabled apps for org: {org_id}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Error disabling apps: {str(e)}")
            return False

    async def __get_or_create_knowledge_base(
        self,
        user_key: str,
        userId: str,
        orgId: str,
        name: str = "Default"
    ) -> dict:
        """Get or create a knowledge base for a user, with root folder and permissions."""
        try:
            if not userId or not orgId:
                self.logger.error("Both User ID and Organization ID are required to get or create a knowledge base")
                return {}

            # Check if a knowledge base already exists for this user in this organization
            query = f"""
            FOR kb IN {CollectionNames.RECORD_GROUPS.value}
                FILTER kb.createdBy == @userId AND kb.orgId == @orgId AND kb.groupType == @kb_type AND kb.connectorName == @kb_connector AND (kb.isDeleted == false OR kb.isDeleted == null)
                RETURN kb
            """
            bind_vars = {
                "userId": userId,
                "orgId": orgId,
                "kb_type": Connectors.KNOWLEDGE_BASE.value,
                "kb_connector": Connectors.KNOWLEDGE_BASE.value,
            }
            cursor = self.arango_service.db.aql.execute(query, bind_vars=bind_vars) # type: ignore
            existing_kbs = [doc for doc in cursor] # type: ignore

            if existing_kbs:
                self.logger.info(f"Found existing knowledge base for user {userId} in organization {orgId}")
                return existing_kbs[0]

            kb_app = await self.__get_or_create_kb_app_for_org(orgId, userId)
            if not kb_app:
                self.logger.error(f"Failed to get or create KB app for org {orgId}")
                return {}
            kb_app_id = kb_app.get('_key')
            current_timestamp = get_epoch_timestamp_in_ms()
            kb_key = str(uuid4())

            kb_data = {
                "_key": kb_key,
                "createdBy": userId,
                "orgId": orgId,
                "groupName": name,
                "groupType": Connectors.KNOWLEDGE_BASE.value,
                "connectorName": Connectors.KNOWLEDGE_BASE.value,
                "connectorId": kb_app_id,  # Link KB to the app
                "createdAtTimestamp": current_timestamp,
                "updatedAtTimestamp": current_timestamp,
            }
            permission_edge = {
                "_from": f"{CollectionNames.USERS.value}/{user_key}",
                "_to": f"{CollectionNames.RECORD_GROUPS.value}/{kb_key}",
                "externalPermissionId": "",
                "type": "USER",
                "role": "OWNER",
                "createdAtTimestamp": current_timestamp,
                "updatedAtTimestamp": current_timestamp,
                "lastUpdatedTimestampAtSource": current_timestamp,
            }

            # Create belongs_to edge from record group to app
            belongs_to_edge = {
                "_from": f"{CollectionNames.RECORD_GROUPS.value}/{kb_key}",
                "_to": f"{CollectionNames.APPS.value}/{kb_app_id}",
                "entityType": Connectors.KNOWLEDGE_BASE.value,
                "createdAtTimestamp": current_timestamp,
                "updatedAtTimestamp": current_timestamp,
            }

            # Insert all in transaction
            # TODO: Use transaction instead of batch upsert
            await self.arango_service.batch_upsert_nodes([kb_data], CollectionNames.RECORD_GROUPS.value)
            await self.arango_service.batch_create_edges([permission_edge], CollectionNames.PERMISSION.value)
            await self.arango_service.batch_create_edges([belongs_to_edge], CollectionNames.BELONGS_TO.value)

            self.logger.info(f"Created new knowledge base for user {userId} in organization {orgId} with app connection")
            return {
                "kb_id": kb_key,
                "name": name,
                "created_at": current_timestamp,
                "updated_at": current_timestamp,
                "app_id": kb_app_id,
                "success": True
            }

        except Exception as e:
            self.logger.error(f"Failed to get or create knowledge base: {str(e)}")
            return {}

    async def __create_kb_connector_app_instance(self, org_id: str, created_by_user_id: Optional[str] = None) -> Optional[dict]:
        """
        Automatically create a Knowledge Base connector instance when an org is created.

        Args:
            org_id: Organization ID
            created_by_user_id: User ID who created the org (optional, can be None for system-created)

        Returns:
            App document if successful, None otherwise
        """
        try:
            self.logger.info(f"📦 Creating Knowledge Base connector instance for org: {org_id}")

            # Get KB connector metadata from the connector class
            from app.connectors.sources.localKB.connector import (
                KB_CONNECTOR_NAME,
                KnowledgeBaseConnector,
            )

            # Check if KB connector metadata exists
            if not hasattr(KnowledgeBaseConnector, '_connector_metadata'):
                self.logger.warning("Knowledge Base connector metadata not found, skipping auto-creation")
                return None

            metadata = KnowledgeBaseConnector._connector_metadata
            connector_name = metadata.get('name', KB_CONNECTOR_NAME)
            app_group = metadata.get('appGroup', 'Local Storage')

            # Check if KB connector instance already exists for this org
            org_apps = await self.arango_service.get_org_apps(org_id)
            existing_kb_app = next(
                (app for app in org_apps if app.get('type') == Connectors.KNOWLEDGE_BASE.value),
                None
            )

            if existing_kb_app:
                self.logger.info(
                    f"Knowledge Base connector instance already exists for org {org_id} "
                    f"(id: {existing_kb_app.get('_key')}), skipping creation"
                )
                return existing_kb_app

            # KB connector uses "NONE" auth type
            selected_auth_type = 'NONE'
            # KB connector is team-scoped
            scope = ConnectorScopes.TEAM.value

            # Use system user if no created_by_user_id provided
            created_by = created_by_user_id if created_by_user_id else "system"

            # Create connector instance document
            instance_key = f"knowledgeBase_{org_id}"
            current_timestamp = get_epoch_timestamp_in_ms()

            instance_document = {
                '_key': instance_key,
                'name': connector_name,
                'type': Connectors.KNOWLEDGE_BASE.value,
                'appGroup': app_group,
                'authType': selected_auth_type,
                'scope': scope,
                'isActive': True,  # KB is always active (local storage)
                'isAgentActive': True,  # KB supports agents
                'isConfigured': True,  # KB doesn't need configuration
                'isAuthenticated': True,  # KB doesn't need authentication (local storage)
                'createdBy': created_by,
                'updatedBy': created_by,
                'createdAtTimestamp': current_timestamp,
                'updatedAtTimestamp': current_timestamp
            }

            # Create instance in database
            await self.arango_service.batch_upsert_nodes(
                [instance_document],
                CollectionNames.APPS.value
            )

            # Create relationship edge between organization and instance
            edge_document = {
                "_from": f"{CollectionNames.ORGS.value}/{org_id}",
                "_to": f"{CollectionNames.APPS.value}/{instance_key}",
                "createdAtTimestamp": current_timestamp,
            }

            await self.arango_service.batch_create_edges(
                [edge_document],
                CollectionNames.ORG_APP_RELATION.value,
            )

            # Create connector instance and add to connectors_map so it is available in-process
            config_service = self.app_container.config_service()
            data_store_provider = await self.app_container.data_store()
            if not hasattr(self.app_container, 'connectors_map'):
                self.logger.info(f"Creating connectors_map for org: {org_id}")
                self.app_container.connectors_map = {}
            connector = await ConnectorFactory.create_and_start_sync(
                name="kb",
                logger=self.logger,
                data_store_provider=data_store_provider,
                config_service=config_service,
                connector_id=instance_key,
            )
            if connector:
                self.app_container.connectors_map[instance_key] = connector
                self.logger.info(
                    f"✅ KB connector instance (id: {instance_key}) added to connectors_map for org: {org_id}"
                )

            self.logger.info(
                f"✅ Successfully created Knowledge Base connector instance '{connector_name}' "
                f"(id: {instance_key}) for org: {org_id}"
            )
            return instance_document

        except Exception as e:
            self.logger.error(f"❌ Error creating KB connector instance for org {org_id}: {str(e)}")
            # Don't fail org creation if KB connector creation fails
            return None

    async def __get_or_create_kb_app_for_org(self, org_id: str, created_by_user_id: Optional[str] = None) -> Optional[dict]:
        """
        Get or create a Knowledge Base connector instance for an org.

        Args:
            org_id: Organization ID
            created_by_user_id: User ID (optional, defaults to "system")

        Returns:
            KB app document or None if failed
        """
        try:
            # Check if KB connector instance already exists for this org
            org_apps = await self.arango_service.get_org_apps(org_id)
            existing_kb_app = next(
                (app for app in org_apps if app.get('type') == Connectors.KNOWLEDGE_BASE.value),
                None
            )

            if existing_kb_app:
                self.logger.debug(f"Found existing KB app for org {org_id}: {existing_kb_app.get('_key')}")
                return existing_kb_app

            # Create KB app if it doesn't exist
            self.logger.info(f"KB app not found for org {org_id}, creating one...")
            new_kb_app = await self.__create_kb_connector_app_instance(org_id, created_by_user_id)
            return new_kb_app

        except Exception as e:
            self.logger.error(f"❌ Error getting or creating KB app for org {org_id}: {str(e)}")
            return None

    async def __create_user_kb_app_relation(self, user_key: str, org_id: str) -> bool:
        """
        Create user-app relation edge between user and KB app for the organization.

        Args:
            user_key: User key
            org_id: Organization ID

        Returns:
            True if edge was created or already exists, False on error
        """
        try:
            # Get or create KB app for the org
            kb_app = await self.__get_or_create_kb_app_for_org(org_id)
            if not kb_app:
                self.logger.warning(f"KB app not found for org {org_id}, skipping user-app relation creation")
                return False

            kb_app_id = kb_app.get('_key')

            # Check if user-app relation already exists
            query = f"""
            FOR edge IN {CollectionNames.USER_APP_RELATION.value}
                FILTER edge._from == CONCAT(@user_collection, '/', @user_key)
                    AND edge._to == CONCAT(@app_collection, '/', @kb_app_id)
                LIMIT 1
                RETURN 1
            """
            bind_vars = {
                "user_key": user_key,
                "kb_app_id": kb_app_id,
                "user_collection": CollectionNames.USERS.value,
                "app_collection": CollectionNames.APPS.value,
            }
            cursor = self.arango_service.db.aql.execute(query, bind_vars=bind_vars)
            if list(cursor):
                # Edge already exists
                self.logger.debug(f"User-app relation already exists for user {user_key} and KB app {kb_app_id}")
                return True

            # Create user-app relation edge
            current_timestamp = get_epoch_timestamp_in_ms()
            user_app_edge = {
                "_from": f"{CollectionNames.USERS.value}/{user_key}",
                "_to": f"{CollectionNames.APPS.value}/{kb_app_id}",
                "syncState": "NOT_STARTED",  # Required by schema - KB doesn't sync
                "lastSyncUpdate": current_timestamp,  # Required by schema
                "createdAtTimestamp": current_timestamp,
                "updatedAtTimestamp": current_timestamp,
            }

            await self.arango_service.batch_create_edges(
                [user_app_edge],
                CollectionNames.USER_APP_RELATION.value
            )

            self.logger.info(f"✅ Created user-app relation for user {user_key} and KB app {kb_app_id}")
            return True

        except Exception as e:
            self.logger.error(f"❌ Error creating user-app relation for user {user_key}: {str(e)}")
            return False
