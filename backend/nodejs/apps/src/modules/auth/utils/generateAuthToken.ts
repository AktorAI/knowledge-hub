import {
  authJwtGenerator,
  fetchConfigJwtGenerator,
} from '../../../libs/utils/createJwt';
import { Org, AccountType } from '../../user_management/schema/org.schema';
import { Logger } from '../../../libs/services/logger.service';

const logger = Logger.getInstance({ service: 'generateAuthToken' });

export async function generateAuthToken(
  user: Record<string, any>,
  jwtSecret: string,
) {
  // Look up org to get accountType for the JWT
  // For JIT-provisioned users, org might not be found immediately - use default
  let accountType: AccountType = 'business'; // Default for JIT users

  try {
    const org = await Org.findOne({ _id: user.orgId, isDeleted: false });
    if (org) {
      accountType = org.accountType;
    } else {
      logger.warn('Org not found for user, using default accountType', {
        userOrgId: user.orgId,
        userEmail: user.email,
        defaultAccountType: accountType,
      });
    }
  } catch (error) {
    logger.warn('Error looking up org, using default accountType', {
      userOrgId: user.orgId,
      error,
    });
  }

  return authJwtGenerator(
    jwtSecret,
    user.email,
    user._id,
    user.orgId,
    user.fullName,
    accountType,
  );
}

export async function generateFetchConfigAuthToken(
  user: Record<string, any>,
  scopedJwtSecret: string,
) {
  return fetchConfigJwtGenerator(user._id, user.orgId, scopedJwtSecret);
}
