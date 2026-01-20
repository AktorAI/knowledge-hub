import { Container } from 'inversify';
import { Logger } from '../../../libs/services/logger.service';
import { RedisService } from '../../../libs/services/redis.service';
import { IamService } from '../services/iam.service';
import { MailService } from '../services/mail.service';
import { SessionService } from '../services/session.service';
import { SamlController } from '../controller/saml.controller';
import { UserAccountController } from '../controller/userAccount.controller';
import { ConfigurationManagerService } from '../services/cm.service';
import { ConfigurationManagerConfig } from '../../configuration_manager/config/config';
import { KeyValueStoreService } from '../../../libs/services/keyValueStore.service';
import { AuthTokenService } from '../../../libs/services/authtoken.service';
import { AuthMiddleware } from '../../../libs/middlewares/auth.middleware';
import { AppConfig } from '../../tokens_manager/config/config';
import { UserController } from '../../user_management/controller/users.controller';
import { AuthService } from '../../user_management/services/auth.service';
import { EntitiesEventProducer } from '../../user_management/services/entity_events.service';

const loggerConfig = {
  service: 'Auth Service Container',
};

export class AuthServiceContainer {
  private static instance: Container;
  private static logger: Logger = Logger.getInstance(loggerConfig);

  static async initialize(
    configurationManagerConfig: ConfigurationManagerConfig,
    appConfig: AppConfig,
  ): Promise<Container> {
    const container = new Container();
    container.bind<Logger>('Logger').toConstantValue(this.logger);
    container
      .bind<ConfigurationManagerConfig>('ConfigurationManagerConfig')
      .toConstantValue(configurationManagerConfig);
    container
      .bind<AppConfig>('AppConfig')
      .toDynamicValue(() => appConfig) // Always fetch latest reference
      .inTransientScope();
    await this.initializeServices(container, appConfig);
    this.instance = container;
    return container;
  }
  private static async initializeServices(
    container: Container,
    appConfig: AppConfig,
  ): Promise<void> {
    try {
      const logger = container.get<Logger>('Logger');
      const redisService = new RedisService(
        appConfig.redis,
        container.get('Logger'),
      );
      container
        .bind<RedisService>('RedisService')
        .toConstantValue(redisService);
      const keyValueStoreService = KeyValueStoreService.getInstance(
        container.get<ConfigurationManagerConfig>('ConfigurationManagerConfig'),
      );

      await keyValueStoreService.connect();
      container
        .bind<KeyValueStoreService>('KeyValueStoreService')
        .toConstantValue(keyValueStoreService);
      const authTokenService = new AuthTokenService(
        appConfig.jwtSecret,
        appConfig.scopedJwtSecret,
      );
      const authMiddleware = new AuthMiddleware(logger, authTokenService);
      container
        .bind<AuthMiddleware>('AuthMiddleware')
        .toConstantValue(authMiddleware);
      const iamService = new IamService(appConfig, logger);
      container.bind<IamService>('IamService').toConstantValue(iamService);
      const mailService = new MailService(appConfig, logger);
      container.bind<MailService>('MailService').toConstantValue(mailService);
      const sessionService = new SessionService(redisService);
      container
        .bind<SessionService>('SessionService')
        .toConstantValue(sessionService);

      const configurationService = new ConfigurationManagerService();
      container
        .bind<ConfigurationManagerService>('ConfigurationManagerService')
        .toConstantValue(configurationService);

      // Add AuthService for UserController
      const authService = new AuthService(appConfig, logger);
      container
        .bind<AuthService>('AuthService')
        .toConstantValue(authService);

      // Add EntitiesEventProducer for UserController
      const entityEventsService = new EntitiesEventProducer(
        appConfig.kafka,
        logger,
      );
      container
        .bind<EntitiesEventProducer>('EntitiesEventProducer')
        .toConstantValue(entityEventsService);

      // Add UserController for JIT provisioning in Microsoft OAuth
      const userController = new UserController(
        appConfig,
        mailService,
        authService,
        logger,
        entityEventsService,
      );
      container
        .bind<UserController>('UserController')
        .toConstantValue(userController);

      container.bind<SamlController>('SamlController').toDynamicValue(() => {
        return new SamlController(iamService, appConfig, logger);
      });

      container
        .bind<UserAccountController>('UserAccountController')
        .toDynamicValue(() => {
          return new UserAccountController(
            appConfig,
            iamService,
            mailService,
            sessionService,
            configurationService,
            logger,
            userController,
          );
        });
    } catch (error) {
      const logger = container.get<Logger>('Logger');
      logger.error('Failed to initialize services', {
        error: error instanceof Error ? error.message : 'Unknown error',
      });
      throw error;
    }
  }

  static getInstance(): Container {
    if (!this.instance) {
      throw new Error('Service container not initialized');
    }
    return this.instance;
  }
  static async dispose(): Promise<void> {
    if (this.instance) {
      try {
        // Get specific services that need to be disconnected
        const redisService = this.instance.isBound('RedisService')
          ? this.instance.get<RedisService>('RedisService')
          : null;

        const keyValueStoreService = this.instance.isBound(
          'KeyValueStoreService',
        )
          ? this.instance.get<KeyValueStoreService>('KeyValueStoreService')
          : null;

        // Disconnect services if they have a disconnect method
        if (redisService && redisService.isConnected()) {
          await redisService.disconnect();
        }

        if (keyValueStoreService && keyValueStoreService.isConnected()) {
          await keyValueStoreService.disconnect();
        }

        const entityEventsService = this.instance.isBound('EntitiesEventProducer')
          ? this.instance.get<EntitiesEventProducer>('EntitiesEventProducer')
          : null;
        if (entityEventsService && entityEventsService.isConnected()) {
          await entityEventsService.stop();
        }

        this.logger.info('All auth services disconnected successfully');
      } catch (error) {
        this.logger.error('Error while disconnecting auth services', {
          error: error instanceof Error ? error.message : 'Unknown error',
        });
      } finally {
        this.instance = null!;
      }
    }
  }
}
